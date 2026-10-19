"""TrustLane web server: REST API, WebSocket push, web pages and /metrics.

In the prototype this one process plays both the store edge gateway and the cloud.
The ops console can switch the "cloud" offline: the gateway then buffers shelf events
and scans, and the exit gate falls back to checking the signed token on its own.

Access control
  * Phone: every trip request carries X-Trip-Auth = HMAC(trip key, METHOD|path|ts); scans are
    signed individually. Trip ids are random.
  * Staff (ops console, staff actions, ERP): per-person sign-in with roles (Authorization: Bearer),
    or the setup PIN (X-Staff-Pin) as a break-glass admin. Every staff action is audit-logged by name.
  * Shelf stations: X-Station-Key when TRUSTLANE_STATION_KEY is set.
  * Exit gate: the Ed25519-signed exit token is the credential.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import logging.handlers
import mimetypes
import os
import secrets
import time
from collections import defaultdict
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

from .accounts import AccountError, Accounts, Mailer, SmsSender
from .alerts import AlertNotifier
from .beacons import ShelfBeacons
from .catalog import Catalog, Product, read_price_csv
from .invoice import render_html
from .config import ROOT, Settings
from .engine import Engine, EngineError, Note
from .ledger import Ledger
from .nfc import GateTags, SunError
from .package_model import PackageModel, PackageModelError
from .payments import payments_from_settings
from .push import PushSender, message_for
from .staff import StaffDirectory, StaffError, StaffUser
from .security import TokenError, TokenSigner, staff_ok, verify_request, verify_token
from .vision_review import Retrainer, ReviewQueue

WEB = ROOT / "web"
# onnxruntime-web loads its WebAssembly through an ES module: browsers insist on these types.
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("application/wasm", ".wasm")
log = logging.getLogger("trustlane")


def _approve_url(auth: dict | None) -> str | None:
    """Where the shopper approves a Reserve Pay mandate: Cashfree's link, or a UPI intent (upi://...)."""
    data = (auth or {}).get("data") or {}
    if not isinstance(data, dict):
        return None
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
    return data.get("url") or payload.get("default") or payload.get("web") or None


# ---------------------------------------------------------------- request bodies
class StartBody(BaseModel):
    shopper_id: str | None = None
    shopper_secret: str | None = None
    consent: bool = False


class LocationBody(BaseModel):
    aisle: str


class ScanBody(BaseModel):
    seq: int
    barcode: str
    ts: int
    sig: str
    package_label: str | None = None
    package_confidence: float | None = None
    offline: bool = False  # the phone queued this scan while it had no connection


class ConfirmBody(BaseModel):
    take: list[str] = []
    deny: list[str] = []


class TapBody(BaseModel):
    token: str
    basket_weight_g: float | None = None  # optional floor scale in the exit lane


class NfcTapBody(BaseModel):
    token: str
    picc_data: str  # from the gate's NTAG 424 DNA tag: encrypted UID + read counter
    cmac: str


class ScaleBody(BaseModel):
    weight_g: float


class GateHeartbeatBody(BaseModel):
    driver: str | None = None
    scale_g: float | None = None
    camera: bool | None = None
    offline_queue: int | None = None
    version: str | None = None


class EdgeHeartbeatBody(BaseModel):
    edge_id: str
    outbox: int = 0
    offline_exits: int = 0
    forwarded: int = 0
    cloud_ok_age_s: float | None = None
    gate_screens: dict = {}


class ShelfEventBody(BaseModel):
    station_id: str
    shelf_id: str
    kind: str
    sku: str | None = None
    lane: int | None = None
    qty: int = 1
    ts: float | None = None
    weight_delta_g: float | None = None
    hand_seen: bool | None = None
    vision_sku: str | None = None
    vision_confidence: float | None = None
    received_ts: float | None = None  # set by the store edge when it forwards an event late (cloud outage)


class HeartbeatBody(BaseModel):
    station_id: str
    shelf_id: str
    lanes: list[dict] | None = None   # [{lane, grams, drift_g, rezeroed}]
    camera_fps: float | None = None
    outbox: int | None = None
    battery_pct: float | None = None
    uptime_s: float | None = None


class PresenceBody(BaseModel):
    station_id: str
    shelf_id: str
    eids: list[str]


class HeardBody(BaseModel):
    beacons: list[dict]  # [{shelf_no, token, rssi}] heard by the phone in the last few seconds


class ResolveBody(BaseModel):
    outcome: str
    take: list[str] = []


class NetworkBody(BaseModel):
    online: bool


class RazorpayConfirmBody(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


class MandateBody(BaseModel):
    shopper_id: str
    shopper_secret: str
    upi_id: str | None = None  # empty: Cashfree returns an approval link / intent instead of a collect
    name: str | None = None
    phone: str | None = None
    email: str | None = None


class MandateStatusBody(BaseModel):
    shopper_id: str
    shopper_secret: str


class PackageCheckBody(BaseModel):
    image_b64: str  # a small JPEG of the item in the shopper's hand; classified in memory, never stored


class LoginBody(BaseModel):
    username: str
    password: str


class StaffCreateBody(BaseModel):
    username: str
    name: str = ""
    role: str
    password: str


class StaffUpdateBody(BaseModel):
    role: str | None = None
    active: bool | None = None
    password: str | None = None


class CouponBody(BaseModel):
    code: str = ""


class PointsBody(BaseModel):
    points: int


class OtpStartBody(BaseModel):
    phone: str


class OtpVerifyBody(BaseModel):
    phone: str
    code: str
    shopper_id: str | None = None      # the phone's current anonymous shopper, kept if the number is new
    shopper_secret: str | None = None


class ShopperAuthBody(BaseModel):
    shopper_id: str
    shopper_secret: str


class ProfileBody(ShopperAuthBody):
    name: str | None = None
    email: str | None = None


class DeleteBody(ShopperAuthBody):
    confirm: bool = False


class ProductBody(BaseModel):
    sku: str
    barcode: str
    name: str
    price: int
    unit_weight_g: float
    theft: str = "low"
    aisle: str
    shelf_id: str | None = None
    lane: int | None = None
    sold_by: str = "unit"
    age_restricted: bool = False
    hsn: str | None = None
    gst_rate: float | None = 18.0
    replacing: str | None = None  # the SKU being edited, when the SKU itself changes


class TakeBody(BaseModel):
    take: list[str] = []


class RefundBody(BaseModel):
    line_ids: list[str]
    reason: str = ""


class DisputeBody(BaseModel):
    text: str


class DisputeResolveBody(BaseModel):
    outcome: str
    note: str = ""
    refund_lines: list[str] = []


class PushTokenBody(BaseModel):
    token: str


class OfflineSyncBody(BaseModel):
    token: str


class AuditBody(BaseModel):
    station_id: str
    shelf_id: str
    lane_sku: str
    camera_sku: str
    ts: float
    jpeg_b64: str


class LabelBody(BaseModel):
    label: str


class RetrainBody(BaseModel):
    epochs: int = 20


class Hub:
    """WebSocket fan-out by channel name."""

    def __init__(self):
        self.channels: dict[str, set[WebSocket]] = defaultdict(set)

    async def join(self, channel: str, ws: WebSocket) -> None:
        await ws.accept()
        self.channels[channel].add(ws)

    def leave(self, channel: str, ws: WebSocket) -> None:
        self.channels[channel].discard(ws)

    async def publish(self, notes: list[Note]) -> None:
        for note in notes:
            for ws in list(self.channels.get(note.channel, ())):
                try:
                    await ws.send_json({"kind": note.kind, "data": note.data})
                except Exception:
                    self.leave(note.channel, ws)


class Gateway:
    """Store edge gateway state: online flag, buffered events, and exits waiting to be charged."""

    def __init__(self):
        self.cloud_online = True
        self.buffer: list[tuple[str, dict, float]] = []
        self.pending_exits: list[tuple[str, str, float]] = []
        self.scales: dict[str, tuple[float, float]] = {}  # gate -> (grams, when): live floor-scale readings

    def snapshot(self) -> dict:
        return {"cloud_online": self.cloud_online, "buffer": self.buffer, "pending_exits": self.pending_exits}

    def restore(self, data: dict) -> None:
        self.cloud_online = data.get("cloud_online", True)
        self.buffer = [tuple(x) for x in data.get("buffer", [])]
        self.pending_exits = [tuple(x) for x in data.get("pending_exits", [])]


class RateLimiter:
    """Token bucket per client and bucket name."""

    def __init__(self):
        self.buckets: dict[tuple[str, str], tuple[float, float]] = {}

    def allow(self, client: str, name: str, rate_per_s: float, burst: float, now: float) -> bool:
        tokens, last = self.buckets.get((client, name), (burst, now))
        tokens = min(burst, tokens + (now - last) * rate_per_s)
        if tokens < 1:
            self.buckets[(client, name)] = (tokens, now)
            return False
        self.buckets[(client, name)] = (tokens - 1, now)
        return True


def _staff_pin(settings: Settings) -> str:
    if settings.staff_pin:
        return settings.staff_pin
    path = Path(settings.data_dir) / "staff_pin.txt"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    pin = f"{secrets.randbelow(10**6):06d}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(pin, encoding="utf-8")
    return pin


def _setup_logging() -> None:
    events = logging.getLogger("trustlane.events")
    if any(isinstance(h, logging.handlers.RotatingFileHandler) for h in events.handlers):
        return
    log_dir = Path(os.getenv("TRUSTLANE_LOG_DIR", ROOT / "logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(log_dir / "trustlane.jsonl", maxBytes=5_000_000,
                                                   backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    events.addHandler(handler)
    if os.getenv("TRUSTLANE_LOG_STDOUT") == "1":  # containers: the platform collects stdout
        out = logging.StreamHandler()
        out.setFormatter(logging.Formatter("%(message)s"))
        events.addHandler(out)
    events.setLevel(logging.INFO)
    events.propagate = False


def _setup_tracing(app: FastAPI) -> None:
    """OpenTelemetry traces to an OTLP collector, when OTEL_EXPORTER_OTLP_ENDPOINT is set."""
    if not os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        log.warning("OTEL_EXPORTER_OTLP_ENDPOINT is set but OpenTelemetry is not installed: "
                    "pip install -r monitoring/requirements-otel.txt")
        return
    provider = TracerProvider(resource=Resource.create({"service.name": "trustlane"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app, excluded_urls="metrics,static,ws")
    log.warning("OpenTelemetry tracing on: exporting to %s", os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"))


def create_app(settings: Settings | None = None, payments=None, notifier: AlertNotifier | None = None,
               push_sender: PushSender | None = None) -> FastAPI:
    settings = settings or Settings()
    data = Path(settings.data_dir)
    ledger = Ledger(data / "ledger.sqlite3")
    engine = Engine(settings, Catalog.load(settings.catalog_path), ledger,
                    TokenSigner(data / "gate_signing_key.pem"), payments or payments_from_settings(settings))
    hub, gateway, limiter = Hub(), Gateway(), RateLimiter()
    notifier = notifier or AlertNotifier(
        settings.slack_webhook, settings.alert_webhook, ledger,
        sms={"sid": settings.twilio_sid, "token": settings.twilio_token, "from": settings.twilio_from,
             "to": [n.strip() for n in settings.twilio_to.split(",") if n.strip()]})
    pusher = push_sender or PushSender()

    def drop_dead_token(token: str) -> None:
        for sess in engine.sessions.values():
            if sess.push_token == token:
                sess.push_token = None
    pusher.on_dead_token = pusher.on_dead_token or drop_dead_token
    dataset = ROOT / "datasets" / "shelf-products"
    review = ReviewQueue(data / "audit", dataset)
    retrainer = Retrainer(ROOT, dataset)
    package_model = PackageModel(settings.package_model_path)
    gate_tags = GateTags(data)
    staff_dir = StaffDirectory(data)
    accounts = Accounts(ledger, SmsSender(settings.twilio_sid, settings.twilio_token, settings.twilio_from),
                        Mailer(settings.smtp_host, settings.smtp_port, settings.smtp_user, settings.smtp_password,
                               settings.smtp_from))

    def email_invoice(sess, invoice: dict) -> None:
        """After each paid trip: e-mail the invoice if the shopper gave an address."""
        profile = ledger.profile(sess.shopper_id) or {}
        if not profile.get("email") or not accounts.mailer.configured:
            return
        doc = ledger.get_invoice(invoice["number"])
        accounts.mailer.send_invoice(profile["email"], engine.store, invoice["number"], invoice["total"],
                                     render_html(invoice["number"], doc["data"]),
                                     settings.public_url.rstrip("/") + invoice["url"])

    engine.invoice_listeners.append(email_invoice)
    beacons = ShelfBeacons(data)
    staff_pin = _staff_pin(settings)
    lock = asyncio.Lock()
    _setup_logging()

    saved = ledger.load_snapshot()
    if saved:
        engine.restore(saved.get("engine", {}))
        gateway.restore(saved.get("gateway", {}))
        log.warning("Restored %d trips from the last snapshot", len(engine.sessions))
    pruned = ledger.prune(time.time() - settings.audit_retention_days * 86400)
    if pruned:
        log.warning("Audit log retention: pruned %d events older than %s days", pruned, settings.audit_retention_days)

    def save() -> None:
        ledger.save_snapshot(time.time(), {"engine": engine.snapshot(), "gateway": gateway.snapshot()})

    def take_snapshot() -> dict:
        return {"engine": engine.snapshot(), "gateway": gateway.snapshot()}  # a deep copy: safe to write later

    # The live-state snapshot covers every trip in memory, so writing it on every request would make each
    # request slower as the day goes on. Requests just mark it dirty; the ticker writes it at most once a
    # second (and once more at shutdown). Every event is in the audit log immediately either way.
    dirty = {"state": False}

    async def mark_dirty() -> None:
        dirty["state"] = True

    async def publish(notes: list[Note]) -> None:
        """WebSocket to every open screen, plus an Expo push to native apps for the notes that matter."""
        await hub.publish(notes)
        if not settings.push_enabled:
            return
        messages = []
        for note in notes:
            if note.channel.startswith("phone:"):
                sess = engine.sessions.get(note.channel.split(":", 1)[1])
                msg = message_for(sess.push_token, note.kind, note.data) if sess and sess.push_token else None
                if msg:
                    messages.append(msg)
        if messages:
            asyncio.create_task(pusher.send(messages))

    async def run(fn, *args, **kwargs):
        """Run one engine call at a time, off the event loop (a payment gateway call can take seconds)."""
        async with lock:
            try:
                result = await asyncio.to_thread(fn, *args, **kwargs)
            except EngineError as err:
                await mark_dirty()
                await publish(err.notes)
                raise HTTPException(err.status, str(err)) from err
            await mark_dirty()
        notes = result[-1] if isinstance(result, tuple) else result
        await publish(notes)
        return result

    async def handle_station(kind: str, body: dict) -> None:
        """Shelf station messages, from HTTP or the MQTT bridge."""
        try:
            if kind == "events":
                await shelf_event_logic(ShelfEventBody(**body))
            elif kind == "heartbeat":
                await heartbeat_logic(HeartbeatBody(**body))
            elif kind == "presence":
                await presence_logic(PresenceBody(**body))
            elif kind == "audit":
                await audit_logic(AuditBody(**body))
        except (HTTPException, ValueError) as err:
            log.warning("Station message %s rejected: %s", kind, err)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        bridge = None
        if settings.mqtt_url:
            from .mqtt_bridge import MqttBridge

            async def from_mqtt(kind: str, body: dict) -> None:
                if settings.station_key and body.pop("station_key", None) != settings.station_key:
                    log.warning("MQTT message without a valid station key ignored")
                    return
                await handle_station(kind, body)

            bridge = MqttBridge(settings.mqtt_url, asyncio.get_running_loop(), from_mqtt)
            bridge.start()
        app.state.mqtt = bridge
        log.warning("Staff PIN for the ops console: %s", staff_pin)

        async def ticker():
            beat = 0
            while True:
                await asyncio.sleep(1)
                beat += 1
                if beat % 60 == 0:
                    async with lock:
                        if (await asyncio.to_thread(engine.compact, time.time()))["archived_trips"]:
                            dirty["state"] = True
                if dirty["state"]:
                    async with lock:  # copy the state under the lock, write it to disk outside it
                        dirty["state"] = False
                        snap = await asyncio.to_thread(take_snapshot)
                    await asyncio.to_thread(ledger.save_snapshot, time.time(), snap)
                if gateway.cloud_online:
                    async with lock:
                        notes = await asyncio.to_thread(engine.tick, time.time())
                        if notes:
                            await mark_dirty()
                    await publish(notes)  # nudges also go out as push notifications
                if beat % 10 == 0:
                    summary = engine.summary(time.time(), gateway.cloud_online, len(gateway.buffer))
                    await notifier.update(summary["alerts"], time.time())
                if beat % 21600 == 0:
                    ledger.prune(time.time() - settings.audit_retention_days * 86400)
                    review.prune(time.time())

        task = asyncio.create_task(ticker())
        yield
        task.cancel()
        async with lock:
            await asyncio.to_thread(save)  # the last state, for a restart
        if bridge:
            bridge.stop()

    app = FastAPI(title="TrustLane prototype", lifespan=lifespan)
    app.state.engine, app.state.hub, app.state.gateway = engine, hub, gateway
    app.state.notifier, app.state.staff_pin = notifier, staff_pin
    app.state.accounts = accounts
    app.mount("/static", StaticFiles(directory=WEB), name="static")
    _setup_tracing(app)

    @app.middleware("http")
    async def no_stale_pages(request: Request, call_next):
        """Pages and scripts revalidate on every load, so a gate or phone never runs an outdated copy."""
        response = await call_next(request)
        path = request.url.path
        if path in ("/", "/phone", "/gate", "/ops", "/shelf", "/checkout") or path.endswith((".html", ".js", ".css")):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.middleware("http")
    async def audit_staff_actions(request: Request, call_next):
        """Every successful staff change goes into the hash-chained audit log under the person's name."""
        response = await call_next(request)
        user = getattr(request.state, "staff", None)
        if user and request.method in ("POST", "PUT", "DELETE") and response.status_code < 400:
            await run(engine.staff_action, user.username, user.role, request.url.path, time.time())
        return response

    @app.middleware("http")
    async def rate_limit(request: Request, call_next):
        if request.url.path.startswith("/api/"):
            client, now = request.client.host if request.client else "?", time.time()
            ok = limiter.allow(client, "api", settings.rate_per_second, 2 * settings.rate_per_second, now)
            if ok and request.method == "POST" and request.url.path == "/api/shoppers/otp/start":
                ok = limiter.allow(client, "otp", 5 / 600, 5, now)  # SMS costs money and can be abused
            if ok and request.method == "POST" and request.url.path == "/api/staff/login":
                ok = limiter.allow(client, "login", 10 / 60, 10, now)  # slow down password guessing
            if ok and request.method == "POST" and request.url.path == "/api/sessions":
                ok = limiter.allow(client, "start", settings.sessions_per_minute / 60, settings.sessions_per_minute, now)
            if not ok:
                return JSONResponse({"detail": "Too many requests, slow down"}, status_code=429)
        return await call_next(request)

    # ---------------------------------------------------------------- access checks
    def trip(sid: str, request: Request, x_trip_auth: str | None = Header(default=None)):
        sess = engine.sessions.get(sid)
        if not sess:
            raise HTTPException(404, "No such shopping session")
        if not verify_request(sess.key, request.method, request.url.path, x_trip_auth, time.time()):
            raise HTTPException(401, "Trip signature missing or not valid")
        return sess

    def staff_user(request: Request, authorization: str | None = Header(default=None),
                   x_staff_pin: str | None = Header(default=None)) -> StaffUser:
        if authorization and authorization.lower().startswith("bearer "):
            try:
                user = staff_dir.verify(authorization[7:].strip(), time.time())
            except StaffError as err:
                raise HTTPException(err.status, str(err)) from err
        elif x_staff_pin and staff_ok(staff_pin, x_staff_pin):
            user = StaffUser("setup-pin", "Setup PIN", "admin")  # break-glass: create real accounts with it
        else:
            raise HTTPException(401, "Staff sign-in needed")
        request.state.staff = user  # picked up by the audit middleware
        return user

    def role(min_role: str):
        def needs(user: StaffUser = Depends(staff_user)) -> StaffUser:
            if not user.can(min_role):
                raise HTTPException(403, f"This needs a {min_role}; you are signed in as {user.role}")
            return user
        return needs

    staff, supervisor, admin = role("associate"), role("supervisor"), role("admin")

    def gate_device(gate_id: str, x_gate_key: str | None = Header(default=None)):
        if settings.gate_key and not secrets.compare_digest(settings.gate_key, x_gate_key or ""):
            raise HTTPException(401, "Gate key is missing or wrong")
        return gate_id

    def check_station(key: str | None):
        if settings.station_key and key != settings.station_key:
            raise HTTPException(401, "Station key is missing or wrong")

    # ---------------------------------------------------------------- pages
    for page in ("phone", "gate", "ops", "shelf"):
        app.add_api_route(f"/{page}", (lambda p=page: FileResponse(WEB / f"{p}.html")), methods=["GET"],
                          include_in_schema=False)

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB / "index.html")

    @app.get("/checkout", include_in_schema=False)
    def checkout_page():
        """Razorpay Checkout for the native app's in-app browser (WebView)."""
        return FileResponse(WEB / "checkout.html")

    # ---------------------------------------------------------------- catalog & sessions
    @app.get("/api/catalog")
    def catalog():
        return [p.public() for p in engine.catalog.all()]

    @app.get("/api/config")
    def config():
        return {"payments": engine.payments.public_config(), "match_window_s": settings.match_window_s,
                "location_fresh_s": settings.location_fresh_s}

    @app.post("/api/sessions")
    async def start(body: StartBody):
        sess, creds, _ = await run(engine.start_session, body.shopper_id, body.shopper_secret, body.consent, time.time())
        out = {"session_id": sess.session_id, "key": sess.key, "match_window_s": settings.match_window_s}
        if creds:
            out["shopper"] = {"shopper_id": creds[0], "shopper_secret": creds[1]}
        return out

    @app.get("/api/sessions/{sid}")
    def get_session(sid: str, _=Depends(trip)):
        return engine.cart_view(sid)

    @app.post("/api/sessions/{sid}/location")
    async def location(sid: str, body: LocationBody, _=Depends(trip)):
        await run(engine.set_location, sid, body.aisle, time.time())
        return {"ok": True}

    @app.post("/api/sessions/{sid}/scans")
    async def scan(sid: str, body: ScanBody):  # each scan carries its own HMAC signature
        if not gateway.cloud_online:
            gateway.buffer.append(("scan", {"session_id": sid, **body.model_dump()}, time.time()))
            await mark_dirty()
            return JSONResponse({"buffered": True}, status_code=202)
        line, _ = await run(engine.scan, sid, body.seq, body.barcode, body.ts, body.sig, time.time(),
                            body.package_label, body.package_confidence, replayed=body.offline)
        return {"line": line.public(), "cart": engine.cart_view(sid)}

    @app.post("/api/sessions/{sid}/checkout")
    async def checkout(sid: str, _=Depends(trip)):
        import segno
        token, _ = await run(engine.checkout, sid, time.time())
        qr = segno.make(token, error="m").svg_inline(scale=4, border=2, dark="#10201B", light="#FFFFFF")
        return {"token": token, "qr_svg": qr}

    @app.post("/api/sessions/{sid}/resume")
    async def resume(sid: str, _=Depends(trip)):
        await run(engine.resume_shopping, sid, time.time())
        return engine.cart_view(sid)

    @app.post("/api/sessions/{sid}/confirm")
    async def confirm(sid: str, body: ConfirmBody, _=Depends(trip)):
        await run(engine.confirm, sid, body.take, body.deny, time.time())
        return engine.cart_view(sid)

    @app.post("/api/sessions/{sid}/pay")
    async def pay(sid: str, _=Depends(trip)):
        await run(engine.pay_in_app, sid, time.time())
        return engine.cart_view(sid)

    @app.post("/api/sessions/{sid}/disputes")
    async def dispute(sid: str, body: DisputeBody, _=Depends(trip)):
        await run(engine.open_dispute, sid, body.text, time.time())
        return {"ok": True}

    @app.post("/api/sessions/{sid}/push-token")
    async def push_token(sid: str, body: PushTokenBody, _=Depends(trip)):
        await run(engine.register_push, sid, body.token, time.time())
        return {"ok": True}

    # ---------------------------------------------------------------- shopper changes their mind
    @app.post("/api/sessions/{sid}/lines/{line_id}/put-back")
    async def put_back(sid: str, line_id: str, _=Depends(trip)):
        await run(engine.put_back, sid, line_id, time.time())
        return engine.cart_view(sid)

    @app.post("/api/sessions/{sid}/picks/{pick_id}/not-me")
    async def not_me(sid: str, pick_id: str, _=Depends(trip)):
        await run(engine.not_me, sid, pick_id, time.time())
        return {"ok": True}

    # ---------------------------------------------------------------- offers, coupons, loyalty points
    @app.post("/api/sessions/{sid}/coupon")
    async def coupon(sid: str, body: CouponBody, _=Depends(trip)):
        await run(engine.apply_coupon, sid, body.code, time.time())
        return engine.cart_view(sid)

    @app.post("/api/sessions/{sid}/points")
    async def points(sid: str, body: PointsBody, _=Depends(trip)):
        await run(engine.set_points, sid, body.points, time.time())
        return engine.cart_view(sid)

    @app.get("/api/offers")
    def public_offers():
        return {"promotions": [{"label": r.get("label", r["id"]), "type": r["type"]} for r in engine.offers.promotions],
                "loyalty": engine.offers.loyalty}

    # ---------------------------------------------------------------- GST invoices
    @app.get("/api/sessions/{sid}/invoice")
    def trip_invoice(sid: str, sess=Depends(trip)):
        inv = (sess.receipt or {}).get("invoice")
        if not inv:
            raise HTTPException(404, "No invoice yet: it is issued when the trip is paid")
        return {**ledger.get_invoice(inv["number"]), "url": inv["url"]}

    @app.get("/invoice/{ref}", include_in_schema=False)
    def invoice_page(ref: str, t: str = ""):
        number = ref.replace("_", "/")
        if not engine.invoicer.check_link(number, t):
            raise HTTPException(403, "This invoice link isn't valid")
        doc = ledger.get_invoice(number)
        if not doc:
            raise HTTPException(404, "No such invoice")
        return Response(render_html(number, doc["data"]), media_type="text/html",
                        headers={"Cache-Control": "private, no-store"})

    @app.get("/api/store")
    def store_info():
        st = engine.store
        return {k: st[k] for k in ("legal_name", "trade_name", "gstin", "address", "state", "grievance_officer")} | {
            "store_id": settings.store_id, "demo": st.get("demo", False)}

    # ---------------------------------------------------------------- shopper accounts (phone sign-in)
    def account_call(fn, *args):
        try:
            return fn(*args)
        except AccountError as err:
            raise HTTPException(err.status, str(err)) from err

    def signed_in(body: ShopperAuthBody) -> str:
        account_call(accounts.check, body.shopper_id, body.shopper_secret)
        return body.shopper_id

    @app.post("/api/shoppers/otp/start")
    async def otp_start(body: OtpStartBody):
        phone = await asyncio.to_thread(account_call, accounts.start_otp, body.phone, time.time())
        return {"sent_to": phone[:-4].replace(phone[3:-4], "*" * len(phone[3:-4])) + phone[-4:], "expires_in": 300}

    @app.post("/api/shoppers/otp/verify")
    async def otp_verify(body: OtpVerifyBody):
        current = (body.shopper_id, body.shopper_secret) if body.shopper_id and body.shopper_secret else None
        result = await asyncio.to_thread(account_call, accounts.verify_otp, body.phone, body.code, current, time.time())
        async with lock:
            engine.ledger.append(time.time(), "account.signin", None, {"new": result["new"]})
        return result

    @app.post("/api/shoppers/me")
    def shopper_me(body: ShopperAuthBody):
        return accounts.me(signed_in(body))

    @app.post("/api/shoppers/me/profile")
    def shopper_profile(body: ProfileBody):
        sid = signed_in(body)
        account_call(accounts.update, sid, body.name, body.email, time.time())
        return accounts.me(sid)

    @app.post("/api/shoppers/me/export")
    def shopper_export(body: ShopperAuthBody):
        """DPDP Act: a copy of everything held about this shopper."""
        sid = signed_in(body)
        data = accounts.export(sid, engine.trips_for(sid), ledger.mandate_for(sid, time.time()))
        return JSONResponse(data, headers={"Content-Disposition": 'attachment; filename="trustlane-my-data.json"'})

    @app.post("/api/shoppers/me/delete")
    async def shopper_delete(body: DeleteBody):
        """DPDP Act: erasure. Invoices stay for 72 months (GST law), no longer linked to the person."""
        sid = signed_in(body)
        if not body.confirm:
            raise HTTPException(400, "Send confirm: true to delete your account")
        counts, _ = await run(engine.erase_shopper, sid, time.time())
        return {"erased": True, **counts}

    # ---------------------------------------------------------------- push test (native app settings)
    @app.post("/api/sessions/{sid}/push-test")
    async def push_test(sid: str, sess=Depends(trip)):
        if not sess.push_token:
            raise HTTPException(409, "This phone hasn't registered for notifications on this trip")
        tickets = await pusher.send([{"to": sess.push_token, "title": "TrustLane", "sound": "default",
                                      "body": "Test notification: nudges will reach you like this.",
                                      "data": {"kind": "test"}}])
        return {"sent": True, "tickets": tickets}

    # ---------------------------------------------------------------- payment gateway
    @app.post("/api/sessions/{sid}/payment/razorpay")
    async def razorpay_confirm(sid: str, body: RazorpayConfirmBody, _=Depends(trip)):
        await run(engine.payment_confirmed, sid, body.razorpay_order_id, body.razorpay_payment_id,
                  body.razorpay_signature, time.time())
        return engine.cart_view(sid)

    @app.post("/api/payments/razorpay/webhook", include_in_schema=False)
    async def razorpay_webhook(request: Request, x_razorpay_signature: str | None = Header(default=None)):
        raw = await request.body()
        if engine.payments.name != "razorpay" or not engine.payments.webhook_ok(raw, x_razorpay_signature):
            raise HTTPException(400, "Webhook signature is not valid")
        event = json.loads(raw)
        payment = event.get("payload", {}).get("payment", {}).get("entity", {})
        await run(engine.payment_webhook, event.get("event", ""), payment, time.time())
        return {"ok": True}

    @app.post("/api/shoppers/mandate")
    async def link_mandate(body: MandateBody):
        """UPI Reserve Pay: block funds once; later trips are debited at the gate with no tap."""
        m, _ = await run(engine.link_mandate, body.shopper_id, body.shopper_secret,
                         {"name": body.name, "phone": body.phone, "email": body.email}, body.upi_id, time.time())
        return {"subscription_id": m["subscription_id"], "status": "PENDING", "approve_url": _approve_url(m.get("auth"))}

    @app.post("/api/shoppers/mandate/status")
    def mandate_status(body: MandateStatusBody):
        try:
            return engine.mandate_status(body.shopper_id, body.shopper_secret, time.time())
        except EngineError as err:
            raise HTTPException(err.status, str(err)) from err

    @app.post("/api/payments/cashfree/webhook", include_in_schema=False)
    async def cashfree_webhook(request: Request, x_webhook_signature: str | None = Header(default=None),
                               x_webhook_timestamp: str | None = Header(default=None)):
        raw = await request.body()
        if engine.payments.name != "cashfree_reserve_pay" or not engine.payments.webhook_ok(
                raw, x_webhook_signature, x_webhook_timestamp, time.time()):
            raise HTTPException(400, "Webhook signature is not valid")
        event = json.loads(raw)
        await run(engine.cashfree_webhook, event.get("type", ""), event.get("data", event), time.time())
        return {"ok": True}

    # ---------------------------------------------------------------- exit gate
    @app.get("/api/gate/public-key")
    def public_key():
        return {"alg": "Ed25519", "key": engine.signer.public_key_b64}

    @app.get("/api/gate/status")
    def gate_status():
        return {"cloud_online": gateway.cloud_online, "gate_key_required": bool(settings.gate_key),
                "nfc": gate_tags.configured}

    async def gate_pass(gate_id: str, token: str, basket_weight_g: float | None) -> dict:
        now = time.time()
        try:
            payload = verify_token(token, engine.signer.public_key, now)
        except TokenError as err:
            await hub.publish([Note(f"gate:{gate_id}", "gate", {"light": "red", "text": f"Token rejected: {err}"})])
            raise HTTPException(401, str(err)) from err
        if basket_weight_g is None and gate_id in gateway.scales:
            grams, at = gateway.scales[gate_id]
            if now - at <= settings.scale_fresh_s:
                basket_weight_g = grams
        if gateway.cloud_online:
            await run(engine.gate_tap, gate_id, payload, now, basket_weight_g)
            return {"mode": "online"}
        # Offline: the gateway trusts only what the signed token says.
        if payload["decision"] == "green":
            gateway.pending_exits.append((payload["sid"], gate_id, now))
            await mark_dirty()
            notes = [Note(f"gate:{gate_id}", "gate", {"light": "green", "text": "Offline check passed. Gate open"}),
                     Note(f"phone:{payload['sid']}", "info", {"text": "Gate opened offline. Your receipt will follow."})]
        else:
            notes = [Note(f"gate:{gate_id}", "gate", {"light": "red", "text": "Offline: a team member will help you"}),
                     Note("ops", "feed", {"ts": now, "kind": "exit.offline", "level": "warn", "session_id": payload["sid"],
                                          "text": f"Offline exit needs staff (token said {payload['decision']})"})]
        await hub.publish(notes)
        return {"mode": "offline", "decision": payload["decision"]}

    @app.post("/api/gate/{gate_id}/tap")
    async def tap(body: TapBody, gate_id: str = Depends(gate_device)):
        """The gate's QR camera (gate screen or gate agent) read a phone's exit token."""
        return await gate_pass(gate_id, body.token, body.basket_weight_g)

    @app.post("/api/gate/nfc-tap")
    async def nfc_tap(body: NfcTapBody):
        """The phone touched a gate's NFC tag. The tag's signed, single-use read proves which gate and
        that the phone is physically there; the exit token proves which trip."""
        try:
            gate_id, _, _ = await asyncio.to_thread(gate_tags.verify, body.picc_data, body.cmac)
        except SunError as err:
            raise HTTPException(401, str(err)) from err
        return {"gate": gate_id, **await gate_pass(gate_id, body.token, None)}

    @app.get("/g", include_in_schema=False)
    def nfc_landing(request: Request):
        """A gate tag's URL. Phones that read the tag outside the app land here and go to the phone app,
        which finishes the tap with its exit token."""
        from fastapi.responses import RedirectResponse
        return RedirectResponse(f"/phone?{request.url.query}", status_code=303)

    @app.post("/api/gate/{gate_id}/heartbeat")
    async def gate_heartbeat(body: GateHeartbeatBody, gate_id: str = Depends(gate_device)):
        engine.gate_heartbeat(gate_id, body.model_dump(exclude_none=True), time.time())
        return {"ok": True}

    @app.post("/api/edge/heartbeat")
    async def edge_heartbeat(body: EdgeHeartbeatBody, x_station_key: str | None = Header(default=None)):
        check_station(x_station_key)
        engine.edge_heartbeat(body.edge_id, body.model_dump(exclude={"edge_id"}), time.time())
        return {"ok": True}

    @app.post("/api/gate/{gate_id}/scale")
    async def gate_scale(body: ScaleBody, gate_id: str = Depends(gate_device)):
        """Live reading from the exit lane's floor scale (gate agent). Used for the next tap at this gate."""
        gateway.scales[gate_id] = (body.weight_g, time.time())
        return {"ok": True}

    @app.post("/api/gate/{gate_id}/offline-sync")
    async def offline_sync(body: OfflineSyncBody, gate_id: str = Depends(gate_device)):
        """A gate that lost the server let a shopper through after checking the token itself.
        Now it's back: bill the trip. Tokens are accepted up to 24 h past expiry for this."""
        now = time.time()
        try:
            payload = verify_token(body.token, engine.signer.public_key, now, grace_s=86400)
        except TokenError as err:
            raise HTTPException(401, str(err)) from err
        if payload["decision"] != "green":
            raise HTTPException(409, "Only green tokens may pass a gate offline")
        if not gateway.cloud_online:
            gateway.pending_exits.append((payload["sid"], gate_id, now))
            await mark_dirty()
            return {"queued": True}
        await run(engine.complete_offline_exit, payload["sid"], gate_id, now)
        return {"billed": True}

    # ---------------------------------------------------------------- model review (camera snapshots)
    async def audit_logic(body: AuditBody):
        try:
            jpeg = base64.b64decode(body.jpeg_b64, validate=True)
            rid = await asyncio.to_thread(review.add, body.station_id, body.shelf_id, body.lane_sku,
                                          body.camera_sku, body.ts, jpeg)
        except ValueError as err:
            raise HTTPException(400, str(err)) from err
        await hub.publish([Note("ops", "review", {"id": rid})])
        return {"id": rid}

    # ---------------------------------------------------------------- package check model (phones)
    @app.get("/api/vision/package-model")
    async def package_model_info():
        try:
            return await asyncio.to_thread(package_model.info)
        except PackageModelError as err:
            return {"available": False, "reason": str(err)}

    @app.get("/api/vision/package-model.onnx", include_in_schema=False)
    def package_model_file():
        if not package_model.available:
            raise HTTPException(404, "No package model on this server")
        return FileResponse(package_model.path, media_type="application/octet-stream",
                            headers={"Cache-Control": "no-cache"})  # revalidated by ETag, so a retrain reaches phones

    @app.post("/api/sessions/{sid}/package-check")
    async def package_check(sid: str, body: PackageCheckBody, _=Depends(trip)):
        """Fallback for phones that can't run the model: classify one photo in memory."""
        if len(body.image_b64) > 2_000_000:
            raise HTTPException(413, "Photo too large: send at most about 1.5 MB")
        try:
            raw = base64.b64decode(body.image_b64.split(",", 1)[-1], validate=True)
            result = await asyncio.to_thread(package_model.classify, raw)
        except ValueError as err:
            raise HTTPException(400, "Photo is not valid base64") from err
        except PackageModelError as err:
            raise HTTPException(503 if "model" in str(err) or "onnxruntime" in str(err) else 400, str(err)) from err
        product = engine.catalog.get(result["label"])
        return {**result, "name": product.name if product else None, "where": "server"}

    @app.post("/api/vision/audit")
    async def vision_audit(body: AuditBody, x_station_key: str | None = Header(default=None)):
        check_station(x_station_key)
        return await audit_logic(body)

    @app.get("/api/vision/review", dependencies=[Depends(staff)])
    def vision_review():
        return {"pending": review.pending()[:50], **review.counts(), "retrain": retrainer.status()}

    @app.get("/api/vision/review/{rid}.jpg", dependencies=[Depends(staff)])
    def vision_image(rid: str):
        path = review.image_path(rid)
        if not path:
            raise HTTPException(404, "No such snapshot")
        return FileResponse(path, media_type="image/jpeg")

    @app.post("/api/vision/review/{rid}", dependencies=[Depends(staff)])
    def vision_label(rid: str, body: LabelBody):
        if body.label not in ("discard", "empty") and not engine.catalog.get(body.label):
            raise HTTPException(400, "Label must be a catalog SKU, 'empty' or 'discard'")
        try:
            return review.label(rid, body.label)
        except KeyError as err:
            raise HTTPException(404, "No such pending snapshot") from err

    @app.post("/api/vision/retrain", dependencies=[Depends(supervisor)])
    def vision_retrain(body: RetrainBody):
        try:
            return retrainer.start(body.epochs)
        except RuntimeError as err:
            raise HTTPException(409, str(err)) from err

    # ---------------------------------------------------------------- shelf stations
    async def shelf_event_logic(body: ShelfEventBody):
        if not gateway.cloud_online:
            gateway.buffer.append(("shelf", body.model_dump(), time.time()))
            await mark_dirty()
            return JSONResponse({"buffered": True}, status_code=202)
        now = time.time()
        # An event the edge held during an outage is matched at the time the store saw it (up to an hour back).
        when = body.received_ts if body.received_ts and now - 3600 <= body.received_ts <= now else now
        await run(engine.shelf_event, body.station_id, body.shelf_id, body.kind, when, sku=body.sku,
                  lane=body.lane, qty=body.qty, event_ts=body.ts, hand_seen=body.hand_seen,
                  weight_delta_g=body.weight_delta_g, vision_sku=body.vision_sku,
                  vision_confidence=body.vision_confidence)
        return {"ok": True}

    async def heartbeat_logic(body: HeartbeatBody):
        info = body.model_dump(exclude={"station_id", "shelf_id"})
        await run(lambda: engine.heartbeat(body.station_id, body.shelf_id, time.time(), info) or [])
        return {"ok": True}

    async def presence_logic(body: PresenceBody):
        await run(engine.presence, body.station_id, body.shelf_id, body.eids, time.time())
        return {"ok": True}

    @app.get("/api/shelf/beacon")
    def shelf_beacon(shelf_id: str, x_station_key: str | None = Header(default=None)):
        """A shelf station's broadcast settings: its number and key for the rotating shelf token."""
        check_station(x_station_key)
        if not any(p.shelf_id == shelf_id for p in engine.catalog.all()):
            raise HTTPException(404, "Unknown shelf")
        return beacons.config(shelf_id)

    @app.post("/api/sessions/{sid}/heard")
    async def heard(sid: str, body: HeardBody, _=Depends(trip)):
        """Shelf tokens this phone heard. Only valid, current tokens count; the strongest signal wins."""
        now = time.time()
        valid = []
        for b in body.beacons[:20]:
            try:
                shelf_id = beacons.verify(int(b.get("shelf_no", 0)), str(b.get("token", "")), now)
            except (TypeError, ValueError):
                shelf_id = None
            if shelf_id:
                valid.append((float(b.get("rssi") or -127), shelf_id))
        if valid:
            await run(engine.heard_shelf, sid, max(valid)[1], now)
        return {"verified": len(valid), "of": len(body.beacons)}

    @app.post("/api/shelf/events")
    async def shelf_event(body: ShelfEventBody, x_station_key: str | None = Header(default=None)):
        check_station(x_station_key)
        return await shelf_event_logic(body)

    @app.post("/api/shelf/heartbeat")
    async def heartbeat(body: HeartbeatBody, x_station_key: str | None = Header(default=None)):
        check_station(x_station_key)
        return await heartbeat_logic(body)

    @app.post("/api/shelf/presence")
    async def presence(body: PresenceBody, x_station_key: str | None = Header(default=None)):
        check_station(x_station_key)
        return await presence_logic(body)

    # ---------------------------------------------------------------- catalog and offers (ops console)
    @app.post("/api/catalog/products", dependencies=[Depends(supervisor)])
    async def save_product(body: ProductBody):
        fields = body.model_dump(exclude={"replacing"})
        try:
            product = Product(**{**fields, "shelf_id": fields["shelf_id"] or None})
            async with lock:
                engine.catalog.upsert(product, body.replacing)
                engine.stock.setdefault(product.sku, 0.0)
                await asyncio.to_thread(engine.catalog.save, settings.catalog_path)
        except (ValueError, TypeError) as err:
            raise HTTPException(400, str(err)) from err
        return product.public()

    @app.delete("/api/catalog/products/{sku}", dependencies=[Depends(supervisor)])
    async def delete_product(sku: str):
        async with lock:
            try:
                engine.catalog.remove(sku)
            except KeyError as err:
                raise HTTPException(404, "No such product") from err
            await asyncio.to_thread(engine.catalog.save, settings.catalog_path)
        return {"deleted": sku}

    @app.get("/api/ops/offers", dependencies=[Depends(staff)])
    def get_offers():
        return engine.offers.to_json()

    @app.put("/api/ops/offers", dependencies=[Depends(supervisor)])
    async def put_offers(request: Request):
        from .pricing import Offers, validate
        raw = await request.json()
        try:
            validate(raw)
        except (ValueError, KeyError, TypeError) as err:
            raise HTTPException(400, f"Offers not saved: {err}") from err
        path = engine.offers_path
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        tmp.replace(path)
        engine.offers = Offers.load(path)
        return engine.offers.to_json()

    # ---------------------------------------------------------------- staff accounts
    @app.post("/api/staff/login")
    async def staff_login(body: LoginBody):
        try:
            token, user = await asyncio.to_thread(staff_dir.login, body.username, body.password, time.time())
        except StaffError as err:
            raise HTTPException(err.status, str(err)) from err
        await run(engine.staff_action, user.username, user.role, "/api/staff/login", time.time())
        return {"token": token, **user.public()}

    @app.get("/api/staff/me")
    def staff_me(user: StaffUser = Depends(staff)):
        return {**user.public(), "accounts": staff_dir.count()}

    @app.get("/api/staff/users", dependencies=[Depends(admin)])
    def staff_users():
        return staff_dir.list()

    @app.post("/api/staff/users", dependencies=[Depends(admin)])
    async def staff_create(body: StaffCreateBody):
        try:
            user = await asyncio.to_thread(staff_dir.add, body.username, body.name, body.role, body.password, time.time())
        except StaffError as err:
            raise HTTPException(err.status, str(err)) from err
        return user.public()

    @app.post("/api/staff/users/{username}")
    async def staff_update(username: str, body: StaffUpdateBody, me: StaffUser = Depends(admin)):
        if username == me.username and (body.active is False or (body.role and body.role != "admin")):
            raise HTTPException(400, "You cannot remove your own admin access")
        try:
            await asyncio.to_thread(staff_dir.update, username, role=body.role, active=body.active, password=body.password)
        except StaffError as err:
            raise HTTPException(err.status, str(err)) from err
        return {"ok": True}

    # ---------------------------------------------------------------- ops (staff only)
    @app.get("/api/ops/summary", dependencies=[Depends(staff)])
    def summary():
        return engine.summary(time.time(), gateway.cloud_online, len(gateway.buffer)) | {
            "buffered_events": len(gateway.buffer), "pending_offline_exits": len(gateway.pending_exits),
            "alerts_sent": notifier.sent[-20:], "mqtt": bool(getattr(app.state, "mqtt", None)),
            "alert_targets": [n for n, v in (("slack", settings.slack_webhook), ("webhook", settings.alert_webhook)) if v]}

    @app.post("/api/ops/checks/{sid}/resolve", dependencies=[Depends(staff)])
    async def resolve(sid: str, body: ResolveBody):
        await run(engine.staff_resolve, sid, body.outcome, body.take, time.time())
        return {"ok": True}

    @app.get("/api/ops/trips/{sid}", dependencies=[Depends(staff)])
    def trip_detail(sid: str):
        try:
            return engine.trip_detail(sid, time.time())
        except EngineError as err:
            raise HTTPException(err.status, str(err)) from err

    @app.post("/api/ops/trips/{sid}/close", dependencies=[Depends(supervisor)])
    async def close_trip(sid: str, body: TakeBody):
        await run(engine.staff_close, sid, body.take, time.time())
        return engine.trip_detail(sid, time.time())

    @app.post("/api/ops/trips/{sid}/cancel", dependencies=[Depends(supervisor)])
    async def cancel_trip(sid: str):
        await run(engine.cancel_trip, sid, time.time())
        return {"ok": True}

    @app.post("/api/ops/trips/{sid}/verify-id", dependencies=[Depends(staff)])
    async def verify_id(sid: str):
        await run(engine.verify_id, sid, time.time())
        return {"ok": True}

    @app.post("/api/ops/trips/{sid}/refund", dependencies=[Depends(supervisor)])
    async def refund(sid: str, body: RefundBody):
        await run(engine.refund, sid, body.line_ids, body.reason, time.time())
        return engine.trip_detail(sid, time.time())

    @app.post("/api/ops/disputes/{did}/resolve", dependencies=[Depends(supervisor)])
    async def resolve_dispute(did: str, body: DisputeResolveBody):
        await run(engine.resolve_dispute, did, body.outcome, body.note, body.refund_lines, time.time())
        return {"ok": True}

    @app.get("/api/ops/presence-sim", dependencies=[Depends(staff)])
    def presence_sim():
        """For the shelf simulator only: the Bluetooth IDs active phones are advertising right now."""
        from .security import eid_for
        now = time.time()
        return [{"session_id": s.session_id, "eid": eid_for(s.key, now), "aisle": s.aisle}
                for s in engine.sessions.values() if s.state == "shopping"]

    @app.post("/api/ops/network", dependencies=[Depends(supervisor)])
    async def network(body: NetworkBody):
        was_online, gateway.cloud_online = gateway.cloud_online, body.online
        replayed = 0
        if body.online and not was_online:
            # Replay buffered events in order, using the time each one reached the gateway.
            for kind, item, received in gateway.buffer:
                try:
                    if kind == "scan":
                        fn = lambda i=item, r=received: engine.scan(  # noqa: E731
                            i["session_id"], i["seq"], i["barcode"], i["ts"], i["sig"], r,
                            i["package_label"], i["package_confidence"], replayed=True)
                    else:
                        fn = lambda i=item, r=received: engine.shelf_event(  # noqa: E731
                            i["station_id"], i["shelf_id"], i["kind"], r, sku=i["sku"], lane=i["lane"], qty=i["qty"],
                            event_ts=i["ts"], hand_seen=i["hand_seen"], weight_delta_g=i["weight_delta_g"],
                            vision_sku=i["vision_sku"], vision_confidence=i["vision_confidence"])
                    await run(fn)
                    replayed += 1
                except HTTPException:
                    pass
            for sid, gate_id, _ in gateway.pending_exits:
                with contextlib.suppress(HTTPException):
                    await run(engine.complete_offline_exit, sid, gate_id, time.time())
            gateway.buffer.clear()
            gateway.pending_exits.clear()
        await mark_dirty()
        await hub.publish([Note("ops", "network", {"online": gateway.cloud_online, "replayed": replayed})])
        return {"online": gateway.cloud_online, "replayed": replayed}

    @app.get("/api/ops/ledger/verify", dependencies=[Depends(supervisor)])
    def verify_ledger():
        return {"intact": engine.ledger.verify_chain(), "events": engine.ledger.count()}

    # ---------------------------------------------------------------- ERP sync (file-based adapter)
    @app.get("/api/erp/stock", dependencies=[Depends(staff)])
    def stock():
        return [{"sku": p.sku, "name": p.name, "stock": engine.stock.get(p.sku, 0),
                 "unit": "kg" if p.by_weight else "units", "price": p.price} for p in engine.catalog.all()]

    @app.post("/api/erp/prices", dependencies=[Depends(supervisor)])
    async def prices(request: Request):
        text = (await request.body()).decode("utf-8")
        async with lock:
            changed = engine.catalog.apply_prices(read_price_csv(text))
            engine.ledger.append(time.time(), "erp.prices", None, {"changed": changed})
        return {"changed": changed}

    @app.get("/api/erp/sales.csv", dependencies=[Depends(supervisor)], response_class=PlainTextResponse)
    def sales():
        rows = ["ts,session_id,sku,price_paise,grams,trip_total_paise"]
        for ts, sid, body in engine.ledger.sales():
            payload = json.loads(body)
            for sku, price, grams in payload.get("items", []):
                rows.append(f"{ts:.0f},{sid},{sku},{price},{'' if grams is None else grams},{payload.get('total', '')}")
        return "\n".join(rows) + "\n"

    @app.get("/metrics", include_in_schema=False)
    def prometheus_metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    # ---------------------------------------------------------------- websockets
    @app.websocket("/ws/{channel}")
    async def ws(channel: str, socket: WebSocket, auth: str | None = None, pin: str | None = None,
                 token: str | None = None):
        allowed = channel.startswith("gate:")  # gate screens only receive light states
        if channel == "ops":
            allowed = bool(pin) and staff_ok(staff_pin, pin)
            if token and not allowed:
                try:
                    staff_dir.verify(token, time.time())
                    allowed = True
                except StaffError:
                    pass
        elif channel.startswith("phone:"):
            sess = engine.sessions.get(channel.split(":", 1)[1])
            allowed = bool(sess) and verify_request(sess.key, "WS", f"/ws/{channel}", auth, time.time())
        if not allowed:
            await socket.close(code=4401)
            return
        await hub.join(channel, socket)
        try:
            while True:
                await socket.receive_text()
        except WebSocketDisconnect:
            hub.leave(channel, socket)

    return app
