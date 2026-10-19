"""TrustLane shelf station.

Reads per-lane weights from an ESP32 + HX711 load cells over USB serial, turns weight
changes into take / put-back events, and sends them to the TrustLane server over HTTP or
MQTT. Optionally watches the shelf front with a camera (MediaPipe hands + YOLO products),
and forwards the rotating Bluetooth IDs of phones the ESP32 hears nearby (presence).

    python -m shelf_station.station --shelf S7-A --serial COM5        # real load cells
    python -m shelf_station.station --shelf S7-A --stdin              # type "take 0" / "return 2 1" / "near <id>"
    python -m shelf_station.station --shelf S7-A --serial COM5 --camera 0 --model models/shelf-classify.pt
    python -m shelf_station.station --shelf S7-A --serial COM5 --mqtt mqtt://gateway:1883

Serial protocol from the firmware, one line each:
    W,<lane>,<grams>     weight reading
    B,<id>,<rssi>        Bluetooth ID heard from a shopper's phone
and to it:
    A,<shelf no><token>  the shelf token to broadcast now (rotates every 30 s); phones that hear it
                         report it, which works even for iPhones in a pocket
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
PRESENCE_RSSI_MIN = -75  # weaker than this is probably the next aisle
PRESENCE_PERIOD_S = 2.0


class Outbox:
    """Events waiting to be delivered. Saved to disk, so a station restart loses nothing."""

    def __init__(self, path: Path | None):
        self.path = path
        self.items: deque[tuple[str, dict]] = deque()
        self.lock = threading.Lock()
        if path and path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    kind, body = json.loads(line)
                    self.items.append((kind, body))
            if self.items:
                print(f"Recovered {len(self.items)} unsent event(s) from {path}")

    def save(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("".join(json.dumps(i) + "\n" for i in self.items), encoding="utf-8")


class Transport:
    """Delivers station messages: kind is events | heartbeat | presence."""

    def __init__(self, outbox: Outbox):
        self.outbox = outbox

    def send(self, kind: str, body: dict) -> None:
        with self.outbox.lock:
            if kind != "heartbeat":  # heartbeats are not worth keeping across an outage
                self.outbox.items.append((kind, body))
                self.outbox.save()
                self._flush()
            else:
                try:
                    self._deliver(kind, body)
                except OSError:
                    pass

    def flush(self) -> None:
        with self.outbox.lock:
            self._flush()

    def _flush(self) -> None:
        while self.outbox.items:
            kind, body = self.outbox.items[0]
            try:
                self._deliver(kind, body)
            except urllib.error.HTTPError as err:
                print(f"  server refused {kind}: {err.code} {err.read().decode(errors='ignore')}", file=sys.stderr)
            except OSError as err:
                print(f"  server unreachable, {len(self.outbox.items)} event(s) queued: {err}", file=sys.stderr)
                return
            self.outbox.items.popleft()
            self.outbox.save()

    def _deliver(self, kind: str, body: dict) -> None:
        raise NotImplementedError


# ---------------------------------------------------------------- shelf token broadcast
BEACON_WINDOW_S = 30


def beacon_token(shelf_key: bytes, now: float) -> str:
    """Same as server.beacons.token_for: HMAC(shelf key, "beacon|" + 30 s window), first 4 bytes."""
    return hmac.new(shelf_key, f"beacon|{int(now // BEACON_WINDOW_S)}".encode(), hashlib.sha256).hexdigest()[:8]


def beacon_frame(config: dict, now: float) -> str:
    return f"A,{config['shelf_no']:04x}{beacon_token(bytes.fromhex(config['key']), now)}"


def beacon_config(server: str, key: str | None, shelf: str) -> dict | None:
    """This shelf's number and token key, from the server (cached on disk for outages)."""
    cache = ROOT / "data" / f"beacon-{shelf}.json"
    req = urllib.request.Request(f"{server.rstrip('/')}/api/shelf/beacon?shelf_id={shelf}",
                                 headers={"X-Station-Key": key} if key else {})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            config = json.loads(resp.read())
        cache.parent.mkdir(exist_ok=True)
        cache.write_text(json.dumps(config))
        return config
    except (urllib.error.URLError, OSError, ValueError) as err:
        if cache.exists():
            return json.loads(cache.read_text())
        print(f"Shelf token disabled: couldn't get this shelf's key ({err})")
        return None


class HttpTransport(Transport):
    PATHS = {"events": "/api/shelf/events", "heartbeat": "/api/shelf/heartbeat", "presence": "/api/shelf/presence",
             "audit": "/api/vision/audit"}

    def __init__(self, base: str, key: str | None, outbox: Outbox):
        super().__init__(outbox)
        self.base, self.key = base.rstrip("/"), key

    def _deliver(self, kind: str, body: dict) -> None:
        req = urllib.request.Request(self.base + self.PATHS[kind], data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json",
                                              **({"X-Station-Key": self.key} if self.key else {})})
        with urllib.request.urlopen(req, timeout=3) as resp:
            resp.read()


class MqttTransport(Transport):
    """Publishes to the store gateway's broker: trustlane/shelf/<shelf>/<kind>, QoS 1."""

    def __init__(self, url: str, shelf: str, key: str | None, outbox: Outbox):
        super().__init__(outbox)
        import paho.mqtt.client as mqtt
        u = urlparse(url)
        self.shelf, self.key = shelf, key
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"trustlane-{shelf}")
        if u.username:
            self.client.username_pw_set(u.username, u.password)
        self.client.connect(u.hostname or "localhost", u.port or 1883)
        self.client.loop_start()

    def _deliver(self, kind: str, body: dict) -> None:
        payload = {**body, **({"station_key": self.key} if self.key else {})}
        info = self.client.publish(f"trustlane/shelf/{self.shelf}/{kind}", json.dumps(payload), qos=1)
        info.wait_for_publish(timeout=3)
        if not info.is_published():
            raise OSError("MQTT broker did not acknowledge the message")


class LaneTracker:
    """Turns a noisy weight stream into takes and put-backs, and watches for scale drift.

    Packaged goods: whole units. Loose produce (by_weight): any settled change of min_change_g or more.
    Drift: a slow change smaller than one unit (temperature, load-cell creep). Above rezero_g it is
    re-zeroed automatically, but only when the camera saw no hand recently (so it never hides a take)."""

    def __init__(self, unit_g: float, window: int = 8, min_noise_g: float = 3.0, by_weight: bool = False,
                 min_change_g: float = 30.0, rezero_g: float = 5.0):
        self.unit = unit_g
        self.by_weight = by_weight
        self.min_change = min_change_g if by_weight else 0.6 * unit_g
        self.readings: deque[float] = deque(maxlen=window)
        self.noise = min_noise_g if by_weight else max(min_noise_g, 0.15 * unit_g)
        self.rezero_g = rezero_g
        self.baseline: float | None = None
        self.stable: float | None = None
        self.last_delta_g = 0.0  # measured grams removed (+) or added (-) for the last event
        self.drift_g = 0.0
        self.rezeroed = False

    def add(self, grams: float, quiet: bool = True) -> int:
        """Returns units removed (+) or added (-) once the reading settles, else 0.
        For loose produce it returns +1/-1 and last_delta_g holds the grams."""
        self.readings.append(grams)
        if len(self.readings) < self.readings.maxlen or max(self.readings) - min(self.readings) > self.noise:
            return 0
        self.stable = stable = sum(self.readings) / len(self.readings)
        if self.baseline is None:
            self.baseline = stable
            return 0
        delta = self.baseline - stable
        if abs(delta) < self.min_change:
            self.drift_g = -delta
            if quiet and abs(self.drift_g) > self.rezero_g:
                self.baseline, self.drift_g, self.rezeroed = stable, 0.0, True
            return 0
        units = (1 if delta > 0 else -1) if self.by_weight else round(delta / self.unit)
        if units:
            self.last_delta_g = delta
            self.baseline = stable
            self.drift_g = 0.0
        return units

    def health(self, lane: int) -> dict:
        return {"lane": lane, "grams": round(self.stable or 0, 1), "drift_g": round(self.drift_g, 1),
                "rezeroed": self.rezeroed}


def prune_audit(audit_dir: Path, days: float = 7) -> None:
    cutoff = time.time() - days * 86400
    for f in audit_dir.rglob("*.jpg"):
        if f.stat().st_mtime < cutoff:
            f.unlink()


def main() -> None:
    ap = argparse.ArgumentParser(description="TrustLane shelf station")
    # 127.0.0.1, not localhost: on Windows "localhost" tries IPv6 first and adds ~2 s per request.
    ap.add_argument("--server", default="http://127.0.0.1:8000")
    ap.add_argument("--mqtt", default=None, help="publish to an MQTT broker instead, e.g. mqtt://gateway:1883")
    ap.add_argument("--shelf", required=True, help="shelf id from the catalog, e.g. S7-A")
    ap.add_argument("--station-id", default=None)
    ap.add_argument("--key", default=None, help="station key, if the server sets TRUSTLANE_STATION_KEY")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--serial", help="serial port of the ESP32, e.g. COM5 or /dev/ttyUSB0")
    src.add_argument("--stdin", action="store_true", help="type events instead of reading load cells")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--camera", default=None,
                    help="camera index (0, 1, ...) or a video file to replay; hands, plus products with --model")
    ap.add_argument("--model", default=None, help="YOLO weights (.pt/.onnx) from shelf_station.train")
    ap.add_argument("--classes", default=None, help="optional JSON file mapping model class names to SKUs")
    ap.add_argument("--vision-conf", type=float, default=0.5, help="minimum YOLO confidence per frame")
    ap.add_argument("--show", action="store_true", help="show the camera preview with recognitions")
    ap.add_argument("--no-hands", action="store_true",
                    help="recognise the whole frame instead of only around hands")
    ap.add_argument("--audit-dir", default=None,
                    help="save hand crops when camera and lane disagree, for review and retraining (kept 7 days)")
    ap.add_argument("--audit-upload", action="store_true",
                    help="also send those crops to the server's model review queue (ops console)")
    args = ap.parse_args()

    station_id = args.station_id or f"station-{args.shelf}"
    started = time.time()
    with urllib.request.urlopen(args.server.rstrip("/") + "/api/catalog", timeout=5) as resp:
        catalog = json.loads(resp.read())
    lanes = {p["lane"]: p for p in catalog if p["shelf_id"] == args.shelf}
    if not lanes:
        sys.exit(f"No monitored products on shelf {args.shelf} in the server catalog")
    print(f"{station_id}: shelf {args.shelf}, lanes " + ", ".join(f"{k}={v['name']}" for k, v in sorted(lanes.items())))

    outbox = Outbox(ROOT / "data" / f"outbox-{station_id}.jsonl")
    transport = MqttTransport(args.mqtt, args.shelf, args.key, outbox) if args.mqtt else \
        HttpTransport(args.server, args.key, outbox)
    print(f"Sending via {'MQTT ' + args.mqtt if args.mqtt else 'HTTP ' + args.server}")

    audit_dir = Path(args.audit_dir) if args.audit_dir else None
    if audit_dir:
        prune_audit(audit_dir)

    camera = None
    if args.camera is not None:
        try:
            from .vision import ProductRecognizer, ShelfCamera
            recognizer = None
            if args.model:
                mapping = json.loads(open(args.classes, encoding="utf-8").read()) if args.classes else None
                model = args.model
                if str(model).endswith(".hef"):  # compiled for the Hailo-8L accelerator
                    from .hailo import HailoClassifier
                    model = HailoClassifier(model)
                recognizer = ProductRecognizer(model, mapping, args.vision_conf)
                print(f"YOLO {recognizer.task} model {args.model}: classes {sorted(set(recognizer.names.values()))}")
            source = int(args.camera) if str(args.camera).isdigit() else args.camera
            camera = ShelfCamera(source, recognizer, show=args.show, use_hands=not args.no_hands)
            camera.start()
            print(f"Camera {args.camera}: hands {'on' if camera.hands else 'off (mediapipe missing)'}, "
                  f"products {'on' if recognizer else 'off (no --model)'}")
        except ImportError as err:
            print(f"Camera skipped ({err}): pip install -r shelf_station/requirements-vision.txt", file=sys.stderr)

    def save_audit(lane_sku: str, seen_sku: str) -> None:
        if not ((audit_dir or args.audit_upload) and camera is not None and camera.last_crop is not None
                and time.time() - camera.last_crop_ts < 5):
            return
        import base64
        import cv2
        if audit_dir:
            folder = audit_dir / time.strftime("%Y-%m-%d") / f"lane-{lane_sku}__camera-{seen_sku}"
            folder.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(folder / f"{int(time.time() * 1000)}.jpg"), camera.last_crop)
        if args.audit_upload:
            ok, jpg = cv2.imencode(".jpg", camera.last_crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if ok:
                transport.send("audit", {"station_id": station_id, "shelf_id": args.shelf, "lane_sku": lane_sku,
                                         "camera_sku": seen_sku, "ts": time.time(),
                                         "jpeg_b64": base64.b64encode(jpg.tobytes()).decode()})

    def emit(lane: int, units: int, delta_g: float | None = None) -> None:
        kind = "take" if units > 0 else "return"
        product = lanes[lane]
        now = time.time()
        default_g = units * product["unit_weight_g"]
        body = {"station_id": station_id, "shelf_id": args.shelf, "lane": lane, "kind": kind, "qty": abs(units),
                "ts": now, "weight_delta_g": -(delta_g if delta_g is not None else default_g)}
        note = f" ({abs(body['weight_delta_g']):.0f} g)" if product.get("sold_by") == "weight" else ""
        if camera:
            seen = camera.evidence.around(now)
            body["hand_seen"] = seen["hand_seen"]
            if kind == "take" and seen["sku"]:
                body["vision_sku"], body["vision_confidence"] = seen["sku"], seen["confidence"]
                agrees = seen["sku"] == product["sku"]
                note += f" (camera: {seen['sku']} {seen['confidence']:.2f} {'agrees' if agrees else 'DISAGREES with lane'})"
                if not agrees:
                    save_audit(product["sku"], seen["sku"])
            else:
                note += f" (camera: hand {'seen' if seen['hand_seen'] else 'NOT seen'}, no product)"
        print(f"{time.strftime('%H:%M:%S')} {kind} x{abs(units)} {product['name']}{note}")
        transport.send("events", body)

    trackers = {lane: LaneTracker(p["unit_weight_g"], by_weight=p.get("sold_by") == "weight")
                for lane, p in lanes.items()}
    heard: dict[str, tuple[int, float]] = {}  # Bluetooth id -> (rssi, last heard)
    heard_lock = threading.Lock()

    def heartbeat_loop() -> None:
        while True:
            transport.send("heartbeat", {
                "station_id": station_id, "shelf_id": args.shelf,
                "lanes": [t.health(lane) for lane, t in trackers.items()],
                "camera_fps": round(camera.fps, 1) if camera else None,
                "outbox": len(outbox.items), "uptime_s": round(time.time() - started)})
            transport.flush()
            time.sleep(10)

    def presence_loop() -> None:
        while True:
            time.sleep(PRESENCE_PERIOD_S)
            now = time.time()
            with heard_lock:
                ids = [i for i, (rssi, ts) in heard.items() if now - ts <= PRESENCE_PERIOD_S and rssi >= PRESENCE_RSSI_MIN]
                for i in [i for i, (_, ts) in heard.items() if now - ts > 30]:
                    del heard[i]
            if ids:
                transport.send("presence", {"station_id": station_id, "shelf_id": args.shelf, "eids": ids})

    threading.Thread(target=heartbeat_loop, daemon=True).start()
    threading.Thread(target=presence_loop, daemon=True).start()

    def quiet() -> bool:
        """No hand at the shelf for 30 s: safe to auto re-zero drift."""
        return camera is None or time.time() - camera.last_hand_ts > 30

    if args.stdin:
        print('Type "take <lane> [qty]", "return <lane> [qty]", "weigh <lane> <grams>" or "near <bluetooth-id>". Ctrl+C to stop.')
        for line in sys.stdin:
            parts = line.lstrip("﻿").split()  # PowerShell pipes can prepend a byte-order mark
            if len(parts) == 2 and parts[0] == "near":
                transport.send("presence", {"station_id": station_id, "shelf_id": args.shelf, "eids": [parts[1]]})
                continue
            try:
                kind, lane = parts[0], int(parts[1])
                qty = float(parts[2]) if len(parts) > 2 else 1
            except (IndexError, ValueError):
                kind, lane, qty = "", -1, 0
            if kind == "weigh" and lane in lanes:  # loose produce: grams taken (+) or put back (-)
                emit(lane, 1 if qty > 0 else -1, qty)
            elif kind in ("take", "return") and lane in lanes:
                emit(lane, int(qty) if kind == "take" else -int(qty))
            elif parts:
                print(f'  not understood: "{line.strip()}". Lanes: {sorted(lanes)}')
        transport.flush()
        return

    import serial  # pyserial

    with serial.Serial(args.serial, args.baud, timeout=1) as port:
        print(f"Reading load cells on {args.serial}")
        beacon = beacon_config(args.server, args.key, args.shelf)

        def beacon_loop() -> None:
            sent = None
            while beacon:
                frame = beacon_frame(beacon, time.time())
                if frame != sent:
                    port.write((frame + "\n").encode())
                    sent = frame
                time.sleep(1)

        threading.Thread(target=beacon_loop, daemon=True).start()
        while True:
            raw = port.readline().decode(errors="ignore").strip()
            try:
                tag, a, b = raw.split(",")
            except ValueError:
                continue
            if tag == "B":
                with heard_lock:
                    heard[a.lower()] = (int(b), time.time())
            elif tag == "W":
                try:
                    lane, grams = int(a), float(b)
                except ValueError:
                    continue
                if lane in trackers:
                    units = trackers[lane].add(grams, quiet=quiet())
                    if units:
                        emit(lane, units, trackers[lane].last_delta_g)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
