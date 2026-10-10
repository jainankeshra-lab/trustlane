"""Shelf camera: MediaPipe finds hands at the shelf front, YOLO recognises the product in them.

Two kinds of YOLO model work:
  * classify (recommended): the crop around each hand is classified as a product or "empty".
    Training data is just photos sorted into folders, made with `python -m shelf_station.capture`.
  * detect: boxes over the whole frame, kept only where they overlap a hand.

Class names must equal catalog SKUs (plus "empty"), or pass a class-to-SKU mapping.
Frames are processed in memory only; nothing is stored or sent, and no face is ever analysed.
"""
from __future__ import annotations

import threading
import time
from collections import Counter, deque
from dataclasses import dataclass
from pathlib import Path

Box = tuple[float, float, float, float]  # x1, y1, x2, y2 in pixels

IGNORED_CLASSES = {"empty", "background", "hand", "none"}


@dataclass
class Sighting:
    ts: float
    sku: str | None  # None: a hand was seen but no product recognised
    confidence: float
    hand: bool


def expand(box: Box, factor: float, width: int, height: int) -> Box:
    """Grow a box around its centre, clamped to the frame. Hands hold products, so look wider."""
    x1, y1, x2, y2 = box
    cx, cy, w, h = (x1 + x2) / 2, (y1 + y2) / 2, (x2 - x1) * factor, (y2 - y1) * factor
    side = max(w, h)
    return (max(0, cx - side / 2), max(0, cy - side / 2), min(width, cx + side / 2), min(height, cy + side / 2))


def overlaps(a: Box, b: Box) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def crop(frame, box: Box):
    x1, y1, x2, y2 = (int(round(v)) for v in box)
    return frame[y1:y2, x1:x2]


def hand_crop_boxes(hand_boxes: list[Box], width: int, height: int, factor: float = 1.8) -> list[Box]:
    return [expand(b, factor, width, height) for b in hand_boxes]


class Evidence:
    """Recent sightings, so a weight change can ask "what did the camera see just now?"."""

    def __init__(self, horizon_s: float = 15.0):
        self.horizon = horizon_s
        self._items: deque[Sighting] = deque()
        self._lock = threading.Lock()

    def add(self, s: Sighting) -> None:
        with self._lock:
            self._items.append(s)
            while self._items and s.ts - self._items[0].ts > self.horizon:
                self._items.popleft()

    def around(self, t: float, before: float = 4.0, after: float = 0.5) -> dict:
        with self._lock:
            window = [s for s in self._items if t - before <= s.ts <= t + after]
        hand_seen = any(s.hand for s in window)
        votes: Counter[str] = Counter()
        seen_in: Counter[str] = Counter()
        for s in window:
            if s.sku:
                votes[s.sku] += s.confidence
                seen_in[s.sku] += 1
        if not votes:
            return {"sku": None, "confidence": 0.0, "hand_seen": hand_seen, "frames": len(window)}
        best, score = votes.most_common(1)[0]
        mean_conf = score / seen_in[best]
        share = score / sum(votes.values())  # penalise frames that disagreed
        return {"sku": best, "confidence": round(mean_conf * share, 3), "hand_seen": hand_seen,
                "frames": len(window)}


class ProductRecognizer:
    """Wraps an Ultralytics YOLO model (classify or detect). `model` may be a path or a loaded model."""

    def __init__(self, model, class_to_sku: dict[str, str] | None = None, min_confidence: float = 0.5):
        if isinstance(model, (str, Path)):
            from ultralytics import YOLO
            model = YOLO(str(model))
        self.model = model
        self.task = getattr(model, "task", "detect")
        names = model.names
        self.names = names if isinstance(names, dict) else dict(enumerate(names))
        self.class_to_sku = class_to_sku or {}
        self.min_confidence = min_confidence

    def _sku(self, class_idx: int) -> str | None:
        name = str(self.names[int(class_idx)])
        if name.lower() in IGNORED_CLASSES:
            return None
        return self.class_to_sku.get(name, name)

    def recognize(self, frame, hand_boxes: list[Box] | None = None) -> list[tuple[str, float]]:
        height, width = frame.shape[:2]
        regions = hand_crop_boxes(hand_boxes, width, height) if hand_boxes else []
        found: list[tuple[str, float]] = []
        if self.task == "classify":
            images = [crop(frame, r) for r in regions] if regions else [frame]
            images = [im for im in images if im.size]
            if not images:
                return []
            for result in self.model(images, verbose=False):
                conf = float(result.probs.top1conf)
                sku = self._sku(int(result.probs.top1))
                if sku and conf >= self.min_confidence:
                    found.append((sku, conf))
            return found
        result = self.model(frame, verbose=False)[0]
        boxes = result.boxes
        for xyxy, cls, conf in zip(boxes.xyxy.tolist(), boxes.cls.tolist(), boxes.conf.tolist()):
            if conf < self.min_confidence:
                continue
            if regions and not any(overlaps(tuple(xyxy), r) for r in regions):
                continue
            sku = self._sku(int(cls))
            if sku:
                found.append((sku, float(conf)))
        return found


HAND_MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
                  "hand_landmarker/float16/latest/hand_landmarker.task")
HAND_MODEL_PATH = Path(__file__).resolve().parent.parent / "models" / "hand_landmarker.task"


class HandFinder:
    """MediaPipe hand boxes, in pixels. Optional: without mediapipe the whole frame is used.

    MediaPipe 1.x has only the Tasks API (HandLandmarker), which needs a small model file; it is
    downloaded once to models/. Older MediaPipe (0.10) falls back to the legacy solutions API."""

    def __init__(self, model_path: Path = HAND_MODEL_PATH):
        import mediapipe as mp
        self._mp = mp
        if hasattr(mp, "solutions"):
            self._legacy = mp.solutions.hands.Hands(max_num_hands=2, min_detection_confidence=0.5)
            return
        self._legacy = None
        from mediapipe.tasks.python import BaseOptions, vision
        if not Path(model_path).exists():
            import urllib.request
            Path(model_path).parent.mkdir(parents=True, exist_ok=True)
            print(f"Downloading MediaPipe hand model to {model_path}")
            urllib.request.urlretrieve(HAND_MODEL_URL, model_path)
        self._landmarker = vision.HandLandmarker.create_from_options(vision.HandLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(model_path)),
            running_mode=vision.RunningMode.IMAGE, num_hands=2, min_hand_detection_confidence=0.5))

    def boxes(self, frame_bgr) -> list[Box]:
        import cv2
        height, width = frame_bgr.shape[:2]
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        if self._legacy is not None:
            hands = [h.landmark for h in self._legacy.process(rgb).multi_hand_landmarks or []]
        else:
            image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
            hands = self._landmarker.detect(image).hand_landmarks
        out = []
        for landmarks in hands:
            xs = [lm.x * width for lm in landmarks]
            ys = [lm.y * height for lm in landmarks]
            out.append((min(xs), min(ys), max(xs), max(ys)))
        return out


class ShelfCamera(threading.Thread):
    """Watches the shelf front and records sightings for the station to query."""

    def __init__(self, camera: int | str, recognizer: ProductRecognizer | None, every_n: int = 2,
                 show: bool = False, use_hands: bool = True):
        """With hands on (default), YOLO runs only when a hand is at the shelf, so products resting
        on the shelf never count. use_hands=False recognises the whole frame, for a camera that
        only sees items once they are lifted."""
        super().__init__(daemon=True)
        import cv2  # noqa: F401  (fail early with a clear message if missing)
        self.camera = camera
        self.recognizer = recognizer
        self.every_n = max(1, every_n)
        self.show = show
        self.evidence = Evidence()
        self.fps = 0.0
        self.last_crop = None   # most recent hand crop, kept in memory for audit samples only
        self.last_crop_ts = 0.0
        self.last_hand_ts = 0.0
        self.hands: HandFinder | None = None
        try:
            if use_hands:
                self.hands = HandFinder()
        except (ImportError, AttributeError, OSError) as err:
            print(f"Hand detection off ({err}); recognising on the whole frame")
            self.hands = None

    def run(self) -> None:
        import cv2
        cap = cv2.VideoCapture(self.camera)
        replay = isinstance(self.camera, str)  # a video file: loop it, at roughly real time
        frame_no, started = 0, time.time()
        while cap.isOpened():
            ok, frame = cap.read()
            if not ok:
                if replay:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                time.sleep(0.05)
                continue
            if replay:
                time.sleep(1 / 15)
            frame_no += 1
            now = time.time()
            boxes = self.hands.boxes(frame) if self.hands else []
            labels: list[tuple[str, float]] = []
            if self.recognizer and frame_no % self.every_n == 0 and (boxes or self.hands is None):
                labels = self.recognizer.recognize(frame, boxes or None)
            if boxes:
                self.last_hand_ts = now
                height, width = frame.shape[:2]
                self.last_crop, self.last_crop_ts = crop(frame, hand_crop_boxes(boxes, width, height)[0]), now
            for sku, conf in labels:
                self.evidence.add(Sighting(now, sku, conf, bool(boxes)))
            if boxes and not labels:
                self.evidence.add(Sighting(now, None, 0.0, True))
            self.fps = frame_no / max(1e-6, now - started)
            if self.show:
                self._draw(frame, boxes, labels)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    self.show = False
                    cv2.destroyAllWindows()

    def _draw(self, frame, boxes: list[Box], labels: list[tuple[str, float]]) -> None:
        import cv2
        height, width = frame.shape[:2]
        for box in hand_crop_boxes(boxes, width, height):
            x1, y1, x2, y2 = (int(v) for v in box)
            cv2.rectangle(frame, (x1, y1), (x2, y2), (90, 160, 40), 2)
        text = ", ".join(f"{sku} {conf:.2f}" for sku, conf in labels) or ("hand" if boxes else "")
        cv2.putText(frame, f"{text}  {self.fps:.0f} fps", (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (20, 20, 20), 3)
        cv2.putText(frame, f"{text}  {self.fps:.0f} fps", (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 1)
        cv2.imshow("TrustLane shelf camera (q to hide)", frame)
