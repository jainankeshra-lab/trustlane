"""Train the shelf camera's YOLO model and copy the best weights to models/.

    # classification (recommended): folders from shelf_station.capture
    python -m shelf_station.train --data datasets/shelf-products

    # detection: a YOLO dataset YAML, e.g. exported from Roboflow or Label Studio
    python -m shelf_station.train --task detect --data datasets/products.yaml

Class names must be catalog SKUs (plus "empty"), so the station can report them directly.
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def class_names(data: Path, task: str) -> list[str]:
    if task == "classify":
        return sorted(p.name for p in (data / "train").iterdir() if p.is_dir())
    import yaml  # installed with ultralytics
    names = yaml.safe_load(data.read_text(encoding="utf-8"))["names"]
    return list(names.values()) if isinstance(names, dict) else list(names)


def check_against_catalog(names: list[str]) -> None:
    catalog = json.loads((ROOT / "data" / "catalog.json").read_text(encoding="utf-8"))
    skus = {p["sku"] for p in catalog["products"]}
    unknown = [n for n in names if n not in skus and n.lower() not in {"empty", "background", "hand", "none"}]
    if unknown:
        print(f"WARNING: classes not in the catalog: {unknown}. Rename the folders/classes to SKUs, "
              f"or give the station a --classes mapping file.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Train the shelf product recogniser")
    ap.add_argument("--data", required=True, help="dataset folder (classify) or dataset YAML (detect)")
    ap.add_argument("--task", choices=["classify", "detect"], default="classify")
    ap.add_argument("--base", default=None, help="starting weights (default yolo11n-cls.pt / yolo11n.pt)")
    ap.add_argument("--epochs", type=int, default=30)
    ap.add_argument("--imgsz", type=int, default=None)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--device", default=None, help="'cpu', '0' for the first GPU, ...")
    ap.add_argument("--export", choices=["onnx", "openvino", "ncnn"], default=None,
                    help="also export for faster CPU / Raspberry Pi inference")
    args = ap.parse_args()

    from ultralytics import YOLO

    data = Path(args.data)
    names = class_names(data, args.task)
    print(f"Classes: {names}")
    check_against_catalog(names)

    base = args.base or ("yolo11n-cls.pt" if args.task == "classify" else "yolo11n.pt")
    imgsz = args.imgsz or (224 if args.task == "classify" else 640)
    model = YOLO(base)
    model.train(data=str(data), epochs=args.epochs, imgsz=imgsz, batch=args.batch, device=args.device,
                project=str(ROOT / "runs" / "shelf"), name=args.task, exist_ok=True, verbose=False)

    best = Path(model.trainer.best)
    target = ROOT / "models" / f"shelf-{args.task}.pt"
    target.parent.mkdir(exist_ok=True)
    shutil.copy(best, target)
    metrics = YOLO(str(target)).val(data=str(data), imgsz=imgsz, verbose=False)
    if args.task == "classify":
        print(f"Validation top-1 accuracy: {metrics.top1:.3f}  (deck target: 0.97 on audited samples)")
    else:
        print(f"Validation mAP50: {metrics.box.map50:.3f}")
    print(f"Model saved to {target}")
    if args.export:
        print("Exported:", YOLO(str(target)).export(format=args.export, imgsz=imgsz))
    if args.task == "classify":
        # The same model checks packages on shoppers' phones (onnxruntime-web / onnxruntime-react-native).
        exported = Path(YOLO(str(target)).export(format="onnx", imgsz=imgsz, simplify=True))
        if exported != target.with_suffix(".onnx"):
            shutil.copy(exported, target.with_suffix(".onnx"))
        print(f"Phone package-check model: {target.with_suffix('.onnx')} (served at /api/vision/package-model.onnx)")
    print(f"Run it:  python -m shelf_station.station --shelf S7-A --serial COM5 --camera 0 --model {target}")


if __name__ == "__main__":
    main()
