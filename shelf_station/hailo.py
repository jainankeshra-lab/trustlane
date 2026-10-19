"""Run the shelf product classifier on a Hailo-8L (Raspberry Pi AI Kit / AI HAT+).

Two parts:

1. Compile (on an x86 Linux PC with Hailo's Dataflow Compiler installed; it is a free download from the
   Hailo Developer Zone after sign-in, and does not run on Windows or on the Pi):

       python -m shelf_station.train --data datasets/shelf-products      # also writes models/shelf-classify.onnx
       python -m shelf_station.hailo compile models/shelf-classify.onnx --calib datasets/shelf-products/train

   This parses the ONNX model, folds the 0..255 -> 0..1 scaling into the network, quantises it to 8 bits
   with ~64-1024 of your own shelf photos as calibration data, and writes models/shelf-classify.hef plus
   models/shelf-classify.names.json.

2. Run (on the Pi, with the hailo-all package from Raspberry Pi OS, which provides HailoRT and
   `hailo_platform`):

       python -m shelf_station.station --shelf S7-A --serial /dev/ttyUSB0 --camera 0 --model models/shelf-classify.hef

`HailoClassifier` behaves like an Ultralytics classify model (`.task`, `.names`, called with a list of
BGR images, results with `.probs.top1` / `.probs.top1conf`), so the rest of the station is unchanged.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np


# ---------------------------------------------------------------- runtime
def square_rgb(image_bgr: np.ndarray, size: int) -> np.ndarray:
    """Same framing as Ultralytics classify: centre square, resized, RGB uint8 (H, W, 3)."""
    import cv2

    h, w = image_bgr.shape[:2]
    side = min(h, w)
    top, left = (h - side) // 2, (w - side) // 2
    square = image_bgr[top:top + side, left:left + side]
    return cv2.cvtColor(cv2.resize(square, (size, size), interpolation=cv2.INTER_LINEAR), cv2.COLOR_BGR2RGB)


def softmax_if_needed(scores: np.ndarray) -> np.ndarray:
    scores = scores.astype(np.float32)
    if np.all(scores >= 0) and abs(float(scores.sum()) - 1.0) < 0.05:
        return scores  # the HEF kept the model's softmax
    e = np.exp(scores - scores.max())
    return e / e.sum()


@dataclass
class _Probs:
    top1: int
    top1conf: float


@dataclass
class _Result:
    probs: _Probs


class HailoRunner:
    """Thin wrapper over HailoRT's InferVStreams pipeline. Input: uint8 NHWC batch. Output: (N, classes)."""

    def __init__(self, hef_path: Path):
        from hailo_platform import (HEF, ConfigureParams, FormatType, HailoStreamInterface, InferVStreams,
                                    InputVStreamParams, OutputVStreamParams, VDevice)

        self._hef = HEF(str(hef_path))
        self._device = VDevice()
        params = ConfigureParams.create_from_hef(self._hef, interface=HailoStreamInterface.PCIe)
        self._group = self._device.configure(self._hef, params)[0]
        self._group_params = self._group.create_params()
        in_params = InputVStreamParams.make(self._group, format_type=FormatType.UINT8)
        out_params = OutputVStreamParams.make(self._group, format_type=FormatType.FLOAT32)
        info = self._hef.get_input_vstream_infos()[0]
        self.input_name, self.size = info.name, int(info.shape[0])
        self.output_name = self._hef.get_output_vstream_infos()[0].name
        self._pipeline = InferVStreams(self._group, in_params, out_params)
        self._pipeline.__enter__()
        self._active = self._group.activate(self._group_params)
        self._active.__enter__()

    def __call__(self, batch: np.ndarray) -> np.ndarray:
        out = self._pipeline.infer({self.input_name: batch})[self.output_name]
        return np.asarray(out).reshape(len(batch), -1)

    def close(self) -> None:
        self._active.__exit__(None, None, None)
        self._pipeline.__exit__(None, None, None)
        self._device.release()


class HailoClassifier:
    """Drop-in for an Ultralytics classify model, running on the Hailo accelerator."""
    task = "classify"

    def __init__(self, hef_path: Path | str, names: list[str] | None = None, runner=None, size: int | None = None):
        hef_path = Path(hef_path)
        if names is None:
            sidecar = hef_path.with_suffix(".names.json")
            if not sidecar.exists():
                raise FileNotFoundError(f"{sidecar} not found: it is written by `python -m shelf_station.hailo compile`")
            names = json.loads(sidecar.read_text(encoding="utf-8"))
        self.names = dict(enumerate(names))
        self.runner = runner or HailoRunner(hef_path)
        self.size = size or getattr(self.runner, "size", 224)

    def __call__(self, images: list[np.ndarray], verbose: bool = False) -> list[_Result]:
        batch = np.stack([square_rgb(im, self.size) for im in images]).astype(np.uint8)
        results = []
        for scores in self.runner(batch):
            probs = softmax_if_needed(np.asarray(scores))
            best = int(probs.argmax())
            results.append(_Result(_Probs(best, float(probs[best]))))
        return results


# ---------------------------------------------------------------- compile (x86 Linux + Hailo DFC)
def load_calibration(folder: Path, size: int, limit: int) -> np.ndarray:
    """Up to `limit` shelf photos (all class folders), framed like inference, float32 NHWC 0..255."""
    import cv2

    files = sorted(p for p in folder.rglob("*") if p.suffix.lower() in (".jpg", ".jpeg", ".png"))
    if not files:
        raise SystemExit(f"No images under {folder}")
    step = max(1, len(files) // limit)
    images = []
    for path in files[::step][:limit]:
        img = cv2.imread(str(path))
        if img is not None:
            images.append(square_rgb(img, size))
    return np.stack(images).astype(np.float32)


def compile_hef(onnx_path: Path, calib_dir: Path, arch: str = "hailo8l", limit: int = 512) -> Path:
    import onnx
    from hailo_sdk_client import ClientRunner  # Hailo Dataflow Compiler

    model = onnx.load(str(onnx_path))
    meta = {p.key: p.value for p in model.metadata_props}
    import ast
    names_map = ast.literal_eval(meta["names"]) if "names" in meta else {}
    names = [names_map[i] for i in sorted(names_map)]
    inp = model.graph.input[0]
    shape = [d.dim_value for d in inp.type.tensor_type.shape.dim]
    size = shape[-1]

    runner = ClientRunner(hw_arch=arch)
    runner.translate_onnx_model(str(onnx_path), onnx_path.stem, start_node_names=[inp.name],
                                net_input_shapes={inp.name: shape})
    # The phone and the ONNX model take 0..1 input; the Hailo input is raw 0..255 pixels.
    runner.load_model_script("normalization1 = normalization([0.0, 0.0, 0.0], [255.0, 255.0, 255.0])\n")
    runner.optimize(load_calibration(calib_dir, size, limit))
    hef = runner.compile()
    out = onnx_path.with_suffix(".hef")
    out.write_bytes(hef)
    out.with_suffix(".names.json").write_text(json.dumps(names), encoding="utf-8")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Hailo-8L support for the shelf classifier")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("compile", help="ONNX -> HEF (needs Hailo's Dataflow Compiler, x86 Linux)")
    c.add_argument("onnx", type=Path)
    c.add_argument("--calib", type=Path, required=True, help="folder of shelf photos for quantisation")
    c.add_argument("--arch", default="hailo8l", choices=["hailo8l", "hailo8"])
    c.add_argument("--limit", type=int, default=512)
    t = sub.add_parser("test", help="classify one image on the Hailo device (on the Pi)")
    t.add_argument("hef", type=Path)
    t.add_argument("image", type=Path)
    args = ap.parse_args()
    if args.cmd == "compile":
        out = compile_hef(args.onnx, args.calib, args.arch, args.limit)
        print(f"Wrote {out} and {out.with_suffix('.names.json')}. Copy both to the Pi's models/ folder.")
    else:
        import cv2
        clf = HailoClassifier(args.hef)
        r = clf([cv2.imread(str(args.image))])[0]
        print(clf.names[r.probs.top1], round(r.probs.top1conf, 3))


if __name__ == "__main__":
    main()
