"""Model monitor: review camera snapshots where YOLO and the load cell disagreed, label them,
and retrain the product recogniser on the corrected labels.

Shelf stations upload a hand crop (never a face, never a full frame) when the camera's product
differs from the lane's. Staff label it on the ops console; labelled crops go straight into the
training set (80/20 train/val split), and "Retrain" runs shelf_station.train in the background.
Unlabelled crops are deleted after 7 days.
"""
from __future__ import annotations

import json
import random
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

MAX_JPEG_BYTES = 500_000


class ReviewQueue:
    def __init__(self, root: Path, dataset: Path, keep_days: float = 7):
        self.root = Path(root)
        self.dataset = Path(dataset)
        self.keep_days = keep_days
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.json"
        self.items: dict[str, dict] = json.loads(self.index_path.read_text("utf-8")) if self.index_path.exists() else {}
        self.lock = threading.Lock()

    def _save(self) -> None:
        self.index_path.write_text(json.dumps(self.items, indent=1), encoding="utf-8")

    def add(self, station_id: str, shelf_id: str, lane_sku: str, camera_sku: str, ts: float, jpeg: bytes) -> str:
        if not jpeg.startswith(b"\xff\xd8") or len(jpeg) > MAX_JPEG_BYTES:
            raise ValueError("Expected a JPEG under 500 KB")
        rid = f"A{int(ts * 1000)}{secrets.token_hex(2)}"
        with self.lock:
            (self.root / f"{rid}.jpg").write_bytes(jpeg)
            self.items[rid] = {"id": rid, "station_id": station_id, "shelf_id": shelf_id, "lane_sku": lane_sku,
                               "camera_sku": camera_sku, "ts": ts, "status": "pending"}
            self._save()
        return rid

    def pending(self) -> list[dict]:
        return sorted((i for i in self.items.values() if i["status"] == "pending"), key=lambda i: -i["ts"])

    def image_path(self, rid: str) -> Path | None:
        path = self.root / f"{rid}.jpg"
        return path if rid in self.items and path.exists() else None

    def label(self, rid: str, label: str, val_share: float = 0.2) -> dict:
        """label: a SKU (or "empty") puts the crop in the training set; "discard" deletes it."""
        with self.lock:
            item = self.items.get(rid)
            if not item or item["status"] != "pending":
                raise KeyError(rid)
            src = self.root / f"{rid}.jpg"
            if label == "discard":
                src.unlink(missing_ok=True)
                item["status"] = "discarded"
            else:
                if not label.replace("-", "").replace("_", "").isalnum():
                    raise ValueError("Label must be a catalog SKU or 'empty'")
                split = "val" if random.random() < val_share else "train"
                dest = self.dataset / split / label
                dest.mkdir(parents=True, exist_ok=True)
                src.replace(dest / f"{rid}.jpg")
                item.update(status="labelled", label=label, split=split)
            self._save()
            return item

    def counts(self) -> dict:
        per_class: dict[str, int] = {}
        if (self.dataset / "train").exists():
            for d in (self.dataset / "train").iterdir():
                if d.is_dir():
                    per_class[d.name] = sum(1 for _ in d.glob("*.jpg"))
        return {"to_review": len(self.pending()), "labelled": sum(1 for i in self.items.values() if i["status"] == "labelled"),
                "training_images": per_class}

    def prune(self, now: float) -> int:
        cutoff, removed = now - self.keep_days * 86400, 0
        with self.lock:
            for rid, item in list(self.items.items()):
                if item["status"] == "pending" and item["ts"] < cutoff:
                    (self.root / f"{rid}.jpg").unlink(missing_ok=True)
                    item["status"] = "expired"
                    removed += 1
            if removed:
                self._save()
        return removed


class Retrainer:
    """Runs `python -m shelf_station.train` in the background and keeps its log."""

    def __init__(self, root: Path, dataset: Path):
        self.root = Path(root)
        self.dataset = Path(dataset)
        self.log_path = self.root / "logs" / "retrain.log"
        self.proc: subprocess.Popen | None = None
        self.started: float | None = None

    def start(self, epochs: int = 20) -> dict:
        if self.proc and self.proc.poll() is None:
            raise RuntimeError("Training is already running")
        if not (self.dataset / "train").exists():
            raise RuntimeError("No labelled images yet: label some snapshots first")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        log = open(self.log_path, "w", encoding="utf-8")
        self.proc = subprocess.Popen([sys.executable, "-m", "shelf_station.train", "--data", str(self.dataset),
                                      "--epochs", str(int(epochs))], cwd=self.root, stdout=log, stderr=subprocess.STDOUT)
        self.started = time.time()
        return self.status()

    def status(self) -> dict:
        tail = ""
        if self.log_path.exists():
            tail = "\n".join(self.log_path.read_text("utf-8", errors="ignore").splitlines()[-12:])
        running = bool(self.proc and self.proc.poll() is None)
        return {"running": running, "returncode": None if running or not self.proc else self.proc.returncode,
                "started": self.started, "log_tail": tail}
