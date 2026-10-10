"""Build a YOLO classification dataset with the shelf camera, in minutes.

    python -m shelf_station.capture --classes razor blades chocolate formula empty

Hold one product in your hand in front of the shelf camera. Press its number key to select the class,
then SPACE to save one photo, or B to toggle burst mode (5 photos a second). Move and rotate the
product while bursting. "empty" should be an empty hand and the bare shelf front.

Photos are cropped around the hand exactly the way the station crops at runtime, then split 80/20
into  <out>/train/<class>/  and  <out>/val/<class>/ , the folder layout Ultralytics expects.
Aim for 150+ photos per class, under the store's real lighting.
"""
from __future__ import annotations

import argparse
import random
import time
from pathlib import Path

from .vision import HandFinder, crop, hand_crop_boxes


def main() -> None:
    import cv2

    ap = argparse.ArgumentParser(description="Capture a product classification dataset")
    ap.add_argument("--classes", nargs="+", required=True, help="catalog SKUs, plus 'empty'")
    ap.add_argument("--out", default="datasets/shelf-products")
    ap.add_argument("--camera", type=int, default=0)
    ap.add_argument("--val", type=float, default=0.2, help="share of photos kept for validation")
    args = ap.parse_args()
    if len(args.classes) > 9:
        raise SystemExit("Up to 9 classes per run (number keys 1-9); run again for more")

    out = Path(args.out)
    for split in ("train", "val"):
        for name in args.classes:
            (out / split / name).mkdir(parents=True, exist_ok=True)
    counts = {c: sum(1 for _ in out.glob(f"*/{c}/*.jpg")) for c in args.classes}

    try:
        hands = HandFinder()
    except (ImportError, AttributeError, OSError):
        hands = None
        print("mediapipe not available: saving whole frames instead of hand crops")

    cap = cv2.VideoCapture(args.camera)
    current, burst, last_shot = 0, False, 0.0
    rng = random.Random()

    def save(frame) -> None:
        nonlocal last_shot
        height, width = frame.shape[:2]
        boxes = hands.boxes(frame) if hands else []
        if hands and not boxes and args.classes[current] != "empty":
            return  # no hand in view: skip, so every product photo matches what the station will see
        image = crop(frame, hand_crop_boxes(boxes, width, height)[0]) if boxes else frame
        if not image.size:
            return
        name = args.classes[current]
        split = "val" if rng.random() < args.val else "train"
        cv2.imwrite(str(out / split / name / f"{name}_{int(time.time() * 1000)}.jpg"), image)
        counts[name] += 1
        last_shot = time.time()

    while cap.isOpened():
        ok, frame = cap.read()
        if not ok:
            continue
        if burst and time.time() - last_shot >= 0.2:
            save(frame)
        view = frame.copy()
        if hands:
            height, width = frame.shape[:2]
            for box in hand_crop_boxes(hands.boxes(frame), width, height):
                x1, y1, x2, y2 = (int(v) for v in box)
                cv2.rectangle(view, (x1, y1), (x2, y2), (90, 160, 40), 2)
        lines = [f"[{i + 1}] {c}: {counts[c]}" + ("  <" if i == current else "") for i, c in enumerate(args.classes)]
        lines.append(f"SPACE photo   B burst {'ON' if burst else 'off'}   Q quit")
        for i, text in enumerate(lines):
            cv2.putText(view, text, (10, 26 + 24 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (20, 20, 20), 3)
            cv2.putText(view, text, (10, 26 + 24 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
        cv2.imshow("TrustLane dataset capture", view)
        key = cv2.waitKey(1) & 0xFF
        if key == ord("q"):
            break
        if ord("1") <= key < ord("1") + len(args.classes):
            current, burst = key - ord("1"), False
        elif key == ord(" "):
            save(frame)
        elif key == ord("b"):
            burst = not burst
    cap.release()
    cv2.destroyAllWindows()
    print("Saved:", ", ".join(f"{c}={n}" for c, n in counts.items()), f"in {out}")


if __name__ == "__main__":
    main()
