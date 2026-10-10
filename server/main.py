"""TrustLane web server: REST API, WebSocket push, web pages and /metrics.

In the prototype this one process plays both the store edge gateway and the cloud.
The ops console can switch the "cloud" offline: the gateway then buffers shelf events
and scans, and the exit gate falls back to checking the signed token on its own.

Access control
  * Phone: every trip request carries X-Trip-Auth = HMAC(trip key, METHOD|path|ts); scans are
    signed individually. Trip ids are random.
  * Staff (ops console, staff actions, ERP): X-Staff-Pin.
  * Shelf stations: X-Station-Key when TRUSTLANE_STATION_KEY is set.
  * Exit gate: the Ed25519-signed exit token is the credential.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import logging.handlers
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

from .alerts import AlertNotifier
from .catalog import Catalog, read_price_csv
from .config import ROOT, Settings
from .engine import Engine, EngineError, Note
from .ledger import Ledger
from .payments import payments_from_settings
from .security import TokenError, TokenSigner, staff_ok, verify_request, verify_token

WEB = ROOT / "web"
log = logging.getLogger("trustlane")


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


class ResolveBody(BaseModel):
    outcome: str
    take: list[str] = []


class NetworkBody(BaseModel):
    online: bool


class RazorpayConfirmBody(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


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
    (ROOT / "logs").mkdir(exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(ROOT / "logs" / "trustlane.jsonl", maxBytes=5_000_000,
                                                   backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    events.addHandler(handler)
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


def create_app(settings: Settings | None = None, payments=None, notifier: AlertNotifier | None = None) -> FastAPI:
    settings = settings or Settings()
    data = Path(settings.data_dir)
    ledger = Ledger(data / "ledger.sqlite3")
    engine = Engine(settings, Catalog.load(settings.catalog_path), ledger,
                    TokenSigner(data / "gate_signing_key.pem"), payments or payments_from_settings(settings))
    hub, gateway, limiter = Hub(), Gateway(), RateLimiter()
    notifier = notifier or AlertNotifier(settings.slack_webhook, settings.alert_webhook, ledger)
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

    async def run(fn, *args, **kwargs):
        """Run one engine call at a time, off the event loop (a payment gateway call can take seconds)."""
        async with lock:
            try:
                result = await asyncio.to_thread(fn, *args, **kwargs)
            except EngineError as err:
                await asyncio.to_thread(save)
                await hub.publish(err.notes)
                raise HTTPException(err.status, str(err)) from err
            await asyncio.to_thread(save)
        notes = result[-1] if isinstance(result, tuple) else result
        await hub.publish(notes)
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
                if gateway.cloud_online:
                    async with lock:
                        notes = await asyncio.to_thread(engine.tick, time.time())
                        if notes:
                            await asyncio.to_thread(save)
                    await hub.publish(notes)
                if beat % 10 == 0:
                    summary = engine.summary(time.time(), gateway.cloud_online, len(gateway.buffer))
                    await notifier.update(summary["alerts"], time.time())
                if beat % 21600 == 0:
                    ledger.prune(time.time() - settings.audit_retention_days * 86400)

        task = asyncio.create_task(ticker())
        yield
        task.cancel()
        if bridge:
            bridge.stop()

    app = FastAPI(title="TrustLane prototype", lifespan=lifespan)
    app.state.engine, app.state.hub, app.state.gateway = engine, hub, gateway
    app.state.notifier, app.state.staff_pin = notifier, staff_pin
    app.mount("/static", StaticFiles(directory=WEB), name="static")
    _setup_tracing(app)

    @app.middleware("http")
    async def rate_limit(request: Request, call_next):
        if request.url.path.startswith("/api/"):
            client, now = request.client.host if request.client else "?", time.time()
            ok = limiter.allow(client, "api", settings.rate_per_second, 2 * settings.rate_per_second, now)
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

    def staff(x_staff_pin: str | None = Header(default=None)):
        if not staff_ok(staff_pin, x_staff_pin):
            raise HTTPException(401, "Staff PIN missing or wrong")

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
            await asyncio.to_thread(save)
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

    # ---------------------------------------------------------------- exit gate
    @app.get("/api/gate/public-key")
    def public_key():
        return {"alg": "Ed25519", "key": engine.signer.public_key_b64}

    @app.get("/api/gate/status")
    def gate_status():
        return {"cloud_online": gateway.cloud_online}

    @app.post("/api/gate/{gate_id}/tap")
    async def tap(gate_id: str, body: TapBody):
        now = time.time()
        try:
            payload = verify_token(body.token, engine.signer.public_key, now)
        except TokenError as err:
            await hub.publish([Note(f"gate:{gate_id}", "gate", {"light": "red", "text": f"Token rejected: {err}"})])
            raise HTTPException(401, str(err)) from err
        if gateway.cloud_online:
            await run(engine.gate_tap, gate_id, payload, now, body.basket_weight_g)
            return {"mode": "online"}
        # Offline: the gateway trusts only what the signed token says.
        if payload["decision"] == "green":
            gateway.pending_exits.append((payload["sid"], gate_id, now))
            await asyncio.to_thread(save)
            notes = [Note(f"gate:{gate_id}", "gate", {"light": "green", "text": "Offline check passed. Gate open"}),
                     Note(f"phone:{payload['sid']}", "info", {"text": "Gate opened offline. Your receipt will follow."})]
        else:
            notes = [Note(f"gate:{gate_id}", "gate", {"light": "red", "text": "Offline: a team member will help you"}),
                     Note("ops", "feed", {"ts": now, "kind": "exit.offline", "level": "warn", "session_id": payload["sid"],
                                          "text": f"Offline exit needs staff (token said {payload['decision']})"})]
        await hub.publish(notes)
        return {"mode": "offline", "decision": payload["decision"]}

    # ---------------------------------------------------------------- shelf stations
    async def shelf_event_logic(body: ShelfEventBody):
        if not gateway.cloud_online:
            gateway.buffer.append(("shelf", body.model_dump(), time.time()))
            await asyncio.to_thread(save)
            return JSONResponse({"buffered": True}, status_code=202)
        await run(engine.shelf_event, body.station_id, body.shelf_id, body.kind, time.time(), sku=body.sku,
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

    @app.post("/api/ops/trips/{sid}/close", dependencies=[Depends(staff)])
    async def close_trip(sid: str, body: TakeBody):
        await run(engine.staff_close, sid, body.take, time.time())
        return engine.trip_detail(sid, time.time())

    @app.post("/api/ops/trips/{sid}/cancel", dependencies=[Depends(staff)])
    async def cancel_trip(sid: str):
        await run(engine.cancel_trip, sid, time.time())
        return {"ok": True}

    @app.post("/api/ops/trips/{sid}/verify-id", dependencies=[Depends(staff)])
    async def verify_id(sid: str):
        await run(engine.verify_id, sid, time.time())
        return {"ok": True}

    @app.post("/api/ops/trips/{sid}/refund", dependencies=[Depends(staff)])
    async def refund(sid: str, body: RefundBody):
        await run(engine.refund, sid, body.line_ids, body.reason, time.time())
        return engine.trip_detail(sid, time.time())

    @app.post("/api/ops/disputes/{did}/resolve", dependencies=[Depends(staff)])
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

    @app.post("/api/ops/network", dependencies=[Depends(staff)])
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
        await asyncio.to_thread(save)
        await hub.publish([Note("ops", "network", {"online": gateway.cloud_online, "replayed": replayed})])
        return {"online": gateway.cloud_online, "replayed": replayed}

    @app.get("/api/ops/ledger/verify", dependencies=[Depends(staff)])
    def verify_ledger():
        return {"intact": engine.ledger.verify_chain(), "events": engine.ledger.count()}

    # ---------------------------------------------------------------- ERP sync (file-based adapter)
    @app.get("/api/erp/stock", dependencies=[Depends(staff)])
    def stock():
        return [{"sku": p.sku, "name": p.name, "stock": engine.stock.get(p.sku, 0),
                 "unit": "kg" if p.by_weight else "units", "price": p.price} for p in engine.catalog.all()]

    @app.post("/api/erp/prices", dependencies=[Depends(staff)])
    async def prices(request: Request):
        text = (await request.body()).decode("utf-8")
        async with lock:
            changed = engine.catalog.apply_prices(read_price_csv(text))
            engine.ledger.append(time.time(), "erp.prices", None, {"changed": changed})
        return {"changed": changed}

    @app.get("/api/erp/sales.csv", dependencies=[Depends(staff)], response_class=PlainTextResponse)
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
    async def ws(channel: str, socket: WebSocket, auth: str | None = None, pin: str | None = None):
        allowed = channel.startswith("gate:")  # gate screens only receive light states
        if channel == "ops":
            allowed = staff_ok(staff_pin, pin)
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
