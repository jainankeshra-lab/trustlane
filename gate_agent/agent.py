"""TrustLane exit gate agent: runs on the gate's Raspberry Pi (or any PC) and drives real hardware.

  * Reads shoppers' exit QR codes with a USB camera (OpenCV) and sends them to the server.
  * Opens the turnstile and sets the tower light when the server's decision arrives (live socket).
  * Streams the exit lane's floor-scale weight, so a heavier-than-scanned basket is noticed.
  * If the server can't be reached, checks the exit token's Ed25519 signature itself, lets green
    trips through, and syncs them later (the same rule as the gate screen).

Hardware, pick one with --driver:
  serial   ESP32 running shelf_station/firmware/esp32_gate (relay, tower light and floor scale)
  gpio     Raspberry Pi pins through gpiozero (relay + three lights; no scale)
  console  prints what it would do (no hardware)

    python -m gate_agent.agent --gate G1 --server http://store-edge:8100 --driver serial --serial COM6 --camera 0
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import threading
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("gate-agent")


# ---------------------------------------------------------------- hardware drivers
class ConsoleDriver:
    def __init__(self):
        self.weight_g: float | None = None
        self.weight_ts = 0.0
        self.events: list[tuple] = []

    def light(self, color: str) -> None:
        self.events.append(("light", color))
        log.info("light %s", color)

    def open(self, ms: int) -> None:
        self.events.append(("open", ms))
        log.info("gate open for %d ms", ms)

    def keepalive(self) -> None:
        pass

    def close(self) -> None:
        pass


class SerialDriver(ConsoleDriver):
    """ESP32 gate board: O,<ms> / L,<G|A|R|-> / T in; S,<grams> out."""
    CODES = {"green": "G", "amber": "A", "red": "R", "": "-"}

    def __init__(self, port: str, baud: int = 115200):
        super().__init__()
        import serial  # pyserial

        self._port = serial.Serial(port, baud, timeout=1)
        self._lock = threading.Lock()
        self._color = ""
        threading.Thread(target=self._read, daemon=True).start()

    def _write(self, line: str) -> None:
        with self._lock:
            self._port.write((line + "\n").encode())

    def _read(self) -> None:
        while True:
            try:
                raw = self._port.readline().decode(errors="replace").strip()
            except Exception as err:  # unplugged
                log.error("Gate board serial error: %s", err)
                time.sleep(1)
                continue
            if raw.startswith("S,"):
                try:
                    self.weight_g, self.weight_ts = float(raw[2:]), time.time()
                except ValueError:
                    pass

    def light(self, color: str) -> None:
        self._color = color
        self._write(f"L,{self.CODES.get(color, '-')}")

    def open(self, ms: int) -> None:
        self._write(f"O,{ms}")

    def keepalive(self) -> None:
        self.light(self._color)  # the board turns amber if the agent goes quiet

    def tare(self) -> None:
        self._write("T")


class GpioDriver(ConsoleDriver):
    def __init__(self, relay: int, green: int, amber: int, red: int):
        super().__init__()
        from gpiozero import LED, OutputDevice

        self._relay = OutputDevice(relay, active_high=True, initial_value=False)
        self._lights = {"green": LED(green), "amber": LED(amber), "red": LED(red)}

    def light(self, color: str) -> None:
        for name, led in self._lights.items():
            led.on() if name == color else led.off()

    def open(self, ms: int) -> None:
        self._relay.on()
        threading.Timer(ms / 1000, self._relay.off).start()


# ---------------------------------------------------------------- the agent
class GateAgent:
    camera_ok: bool | None = None  # set by the camera loop

    def __init__(self, server: str, gate: str, driver, gate_key: str = "", open_ms: int = 3000,
                 client: httpx.Client | None = None, state_dir: Path | None = None, scale_fresh_s: float = 3.0):
        self.server, self.gate, self.driver = server.rstrip("/"), gate, driver
        self.open_ms = open_ms
        self.scale_fresh_s = scale_fresh_s
        self.client = client or httpx.Client(base_url=self.server, timeout=5)
        self.headers = {"X-Gate-Key": gate_key} if gate_key else {}
        self.state_dir = Path(state_dir or ROOT / "data")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.queue_path = self.state_dir / f"gate-{gate}-offline.jsonl"
        self.key_path = self.state_dir / f"gate-{gate}-public-key.txt"
        self._seen: dict[str, float] = {}
        self._public_key = None
        self._last_scale_sent: float | None = None

    # -- server decisions (live socket)
    def on_message(self, msg: dict) -> None:
        if msg.get("kind") != "gate":
            return
        light = msg["data"].get("light", "")
        self.driver.light(light)
        if light == "green":
            self.driver.open(self.open_ms)

    # -- floor scale
    def scale_reading(self) -> float | None:
        w = self.driver.weight_g
        return w if w is not None and time.time() - self.driver.weight_ts <= self.scale_fresh_s else None

    def push_scale(self) -> None:
        """Keep the server's view of the floor scale fresh (used when a phone taps by NFC)."""
        w = self.scale_reading()
        if w is None or (self._last_scale_sent is not None and abs(w - self._last_scale_sent) < 20):
            return
        try:
            self.client.post(f"/api/gate/{self.gate}/scale", json={"weight_g": w}, headers=self.headers)
            self._last_scale_sent = w
        except httpx.HTTPError:
            pass

    def heartbeat(self, camera_ok: bool | None) -> None:
        """Every 10 s: lets the ops console and Prometheus see this gate is alive."""
        queued = sum(1 for ln in self.queue_path.read_text(encoding="utf-8").splitlines() if ln.strip()) \
            if self.queue_path.exists() else 0
        body = {"driver": type(self.driver).__name__.replace("Driver", "").lower(), "scale_g": self.scale_reading(),
                "camera": camera_ok, "offline_queue": queued, "version": "1"}
        try:
            self.client.post(f"/api/gate/{self.gate}/heartbeat", json=body, headers=self.headers)
        except httpx.HTTPError:
            pass

    # -- exit codes from the camera
    def submit(self, token: str) -> str:
        """One exit QR read. Returns what happened: online / offline-open / offline-refused / rejected / duplicate."""
        now = time.time()
        if now - self._seen.get(token, 0) < 8:
            return "duplicate"  # the same code is still in front of the camera
        self._seen = {t: ts for t, ts in self._seen.items() if now - ts < 60}
        self._seen[token] = now
        try:
            r = self.client.post(f"/api/gate/{self.gate}/tap", headers=self.headers,
                                 json={"token": token, "basket_weight_g": self.scale_reading()})
        except httpx.HTTPError:
            return self._offline(token, now)
        if r.status_code >= 500:
            return self._offline(token, now)
        if r.status_code >= 400:
            self.driver.light("red")
            log.warning("Exit code rejected: %s", r.text)
            return "rejected"
        return "online"  # the decision arrives on the live socket

    def _load_key(self):
        if self._public_key is None:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            if not self.key_path.exists():
                return None
            raw = self.key_path.read_text().strip()
            self._public_key = Ed25519PublicKey.from_public_bytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        return self._public_key

    def refresh_key(self) -> None:
        try:
            key = self.client.get("/api/gate/public-key").json()["key"]
            self.key_path.write_text(key)
            self._public_key = None
        except (httpx.HTTPError, ValueError, KeyError):
            pass  # keep the cached key

    def _offline(self, token: str, now: float) -> str:
        from server.security import TokenError, verify_token

        key = self._load_key()
        if key is None:
            self.driver.light("red")
            return "offline-refused"
        try:
            payload = verify_token(token, key, now)
        except TokenError:
            self.driver.light("red")
            return "rejected"
        if payload.get("decision") != "green":
            self.driver.light("red")
            return "offline-refused"
        with self.queue_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"token": token}) + "\n")
        self.driver.light("green")
        self.driver.open(self.open_ms)
        log.warning("Server unreachable: gate opened after checking the signed code locally (trip %s)", payload["sid"])
        return "offline-open"

    def sync_offline(self) -> int:
        if not self.queue_path.exists():
            return 0
        lines = [ln for ln in self.queue_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        left, done = [], 0
        for i, ln in enumerate(lines):
            try:
                r = self.client.post(f"/api/gate/{self.gate}/offline-sync", json=json.loads(ln), headers=self.headers)
            except httpx.HTTPError:
                left = lines[i:]
                break
            if r.status_code >= 500:
                left = lines[i:]
                break
            done += 1  # billed, queued by the gateway, or permanently refused: either way it's handled
        self.queue_path.write_text("".join(ln + "\n" for ln in left), encoding="utf-8")
        return done


# ---------------------------------------------------------------- loops
def camera_loop(agent: GateAgent, source: str, stop: threading.Event) -> None:
    import cv2

    cap = cv2.VideoCapture(int(source) if source.isdigit() else source)
    detector = cv2.QRCodeDetector()
    if not cap.isOpened():
        log.error("Camera %s could not be opened", source)
        agent.camera_ok = False
        return
    agent.camera_ok = True
    log.info("Reading exit codes from camera %s", source)
    while not stop.is_set():
        ok, frame = cap.read()
        if not ok:
            time.sleep(0.2)
            continue
        text, _, _ = detector.detectAndDecode(frame)
        if text and text.count(".") == 1:
            log.info("Exit code read: %s", agent.submit(text))
    cap.release()


async def socket_loop(agent: GateAgent, stop: threading.Event) -> None:
    import websockets

    url = agent.server.replace("http", "ws", 1) + f"/ws/gate:{agent.gate}"
    while not stop.is_set():
        try:
            async with websockets.connect(url) as ws:
                log.info("Connected to %s", url)
                agent.refresh_key()
                async for raw in ws:
                    agent.on_message(json.loads(raw))
        except Exception as err:
            log.warning("Live socket down (%s); retrying", err)
            await asyncio.sleep(2)


def housekeeping(agent: GateAgent, stop: threading.Event) -> None:
    beat = 0
    while not stop.wait(1):
        beat += 1
        agent.push_scale()
        if beat % 10 == 0:
            agent.driver.keepalive()
            agent.heartbeat(agent.camera_ok)
            if agent.sync_offline():
                log.info("Synced offline exits")


def main() -> None:
    ap = argparse.ArgumentParser(description="TrustLane exit gate agent")
    ap.add_argument("--server", default="http://127.0.0.1:8000", help="store edge (or cloud) server")
    ap.add_argument("--gate", default="G1")
    ap.add_argument("--key", default="", help="gate key, if the server sets TRUSTLANE_GATE_KEY")
    ap.add_argument("--driver", choices=["serial", "gpio", "console"], default="console")
    ap.add_argument("--serial", help="ESP32 gate board port, e.g. COM6 or /dev/ttyUSB0")
    ap.add_argument("--gpio", default="17,22,23,24", help="relay,green,amber,red BCM pins (gpio driver)")
    ap.add_argument("--camera", default=None, help="camera index or video file for reading exit QR codes")
    ap.add_argument("--open-ms", type=int, default=3000)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.driver == "serial":
        if not args.serial:
            raise SystemExit("--driver serial needs --serial PORT")
        driver = SerialDriver(args.serial)
    elif args.driver == "gpio":
        driver = GpioDriver(*[int(x) for x in args.gpio.split(",")])
    else:
        driver = ConsoleDriver()
    agent = GateAgent(args.server, args.gate, driver, args.key, args.open_ms)
    agent.refresh_key()
    stop = threading.Event()
    threading.Thread(target=housekeeping, args=(agent, stop), daemon=True).start()
    if args.camera is not None:
        threading.Thread(target=camera_loop, args=(agent, args.camera, stop), daemon=True).start()
    try:
        asyncio.run(socket_loop(agent, stop))
    except KeyboardInterrupt:
        stop.set()


if __name__ == "__main__":
    main()
