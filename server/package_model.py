"""Package check model.

The shelf camera's product classifier (shelf_station.train), exported to ONNX, does a second job: the
shopper's phone downloads it and checks that the package in their hand matches the barcode they
scanned. It runs on the phone (onnxruntime-web in the browser, onnxruntime-react-native in the app),
so the photo never leaves the phone. Phones that can't run it may send a small photo here instead;
it is classified in memory and never stored.

Preprocessing matches Ultralytics' classify transforms: scale the shorter side to the input size,
centre-crop a square, RGB, values 0..1, NCHW. The model's output is already softmax probabilities.
"""
from __future__ import annotations

import ast
import hashlib
import io
import threading
from pathlib import Path


class PackageModelError(Exception):
    pass


def preprocess(image, size: int):
    """PIL image -> float32 array [1, 3, size, size]."""
    import numpy as np
    from PIL import Image

    img = image.convert("RGB")
    w, h = img.size
    scale = size / min(w, h)
    img = img.resize((max(size, round(w * scale)), max(size, round(h * scale))), Image.BILINEAR)
    w, h = img.size
    left, top = (w - size) // 2, (h - size) // 2
    img = img.crop((left, top, left + size, top + size))
    return (np.asarray(img, dtype=np.float32) / 255.0).transpose(2, 0, 1)[None]


class PackageModel:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._session = None
        self._lock = threading.Lock()
        self._info: dict | None = None

    @property
    def available(self) -> bool:
        return self.path.exists()

    def _load(self):
        if self._session is None:
            if not self.available:
                raise PackageModelError("No package model on this server (train one with shelf_station.train)")
            try:
                import onnxruntime as ort
            except ImportError as err:
                raise PackageModelError("onnxruntime is not installed on the server") from err
            self._session = ort.InferenceSession(str(self.path), providers=["CPUExecutionProvider"])
        return self._session

    def info(self) -> dict:
        """What a phone needs to run the model itself."""
        if not self.available:
            return {"available": False}
        if self._info is None:
            sess = self._load()
            meta = sess.get_modelmeta().custom_metadata_map
            names = ast.literal_eval(meta["names"]) if "names" in meta else {}
            shape = sess.get_inputs()[0].shape
            data = self.path.read_bytes()
            self._info = {
                "available": True,
                "names": [names[i] for i in sorted(names)],
                "input": {"name": sess.get_inputs()[0].name, "size": int(shape[-1])},
                "preprocess": "shorter side to size, centre crop, RGB 0..1, NCHW",
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
                "url": "/api/vision/package-model.onnx",
            }
        return self._info

    def classify(self, image_bytes: bytes) -> dict:
        from PIL import Image, UnidentifiedImageError

        info = self.info()
        if not info["available"]:
            raise PackageModelError("No package model on this server")
        try:
            image = Image.open(io.BytesIO(image_bytes))
            image.load()
        except (UnidentifiedImageError, OSError) as err:
            raise PackageModelError("That photo couldn't be read") from err
        x = preprocess(image, info["input"]["size"])
        with self._lock:
            probs = self._load().run(None, {info["input"]["name"]: x})[0][0]
        best = int(probs.argmax())
        return {"label": info["names"][best], "confidence": round(float(probs[best]), 4)}
