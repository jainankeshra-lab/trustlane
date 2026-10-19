"""TrustLane store edge gateway: a separate process inside the store, between in-store devices and the cloud.

Shelf stations, the gate screen and the gate agent talk to the edge on the store network; phones talk
to the cloud directly (and queue their own scans offline). The edge:

  * takes shelf messages (events, heartbeats, Bluetooth presence, camera audits) into a durable SQLite
    outbox and forwards them to the cloud in order. During a cloud outage they wait on disk; each
    event keeps the time the store received it, so the cloud still matches picks to scans correctly.
  * forwards gate taps and floor-scale readings to the cloud. If the cloud can't be reached, it checks
    the exit token's Ed25519 signature itself (public key cached on disk), opens the gate for green
    trips, and bills them through the cloud's offline-sync once it is back.
  * relays the cloud's gate decisions to the gate screens and agents connected to it, and adds its
    own decisions while offline.

Same API paths as the cloud for these devices, so a station or gate just points at the edge:

    python -m server.edge --cloud https://trustlane-cloud.example --port 8100
    python -m shelf_station.station --shelf S7-A --serial COM5 --server http://<edge>:8100
    python -m gate_agent.agent --gate G1 --server http://<edge>:8100 --driver serial --serial COM6
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import logging
import os
import sqlite3
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from .security import TokenError, verify_token

log = logging.getLogger("trustlane.edge")
STATION_KINDS = {"events": "/api/shelf/events", "heartbeat": "/api/shelf/heartbeat",
                 "presence": "/api/shelf/presence", "audit": "/api/vision/audit"}


class Outbox:
    """Durable, ordered store-and-forward queue (SQLite)."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL, "
                         "body TEXT NOT NULL, headers TEXT NOT NULL, received REAL NOT NULL, tries INTEGER NOT NULL DEFAULT 0)")
        self._db.commit()

    def add(self, path: str, body: dict, headers: dict, received: float) -> None:
        self._db.execute("INSERT INTO outbox (path, body, headers, received) VALUES (?,?,?,?)",
                         (path, json.dumps(body), json.dumps(headers), received))
        self._db.commit()

    def head(self, n: int = 50) -> list[tuple]:
        return self._db.execute("SELECT id, path, body, headers, received FROM outbox ORDER BY id LIMIT ?", (n,)).fetchall()

    def done(self, row_id: int) -> None:
        self._db.execute("DELETE FROM outbox WHERE id=?", (row_id,))
        self._db.commit()

    def size(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]


class EdgeState:
    def __init__(self, cloud: str, data_dir: Path, gate_key: str, client: httpx.AsyncClient | None):
        self.cloud = cloud.rstrip("/")
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.gate_key = gate_key
        self.client = client or httpx.AsyncClient(base_url=self.cloud, timeout=4)
        self.outbox = Outbox(self.data_dir / "outbox.sqlite3")
        self.exits = Outbox(self.data_dir / "offline_exits.sqlite3")
        self.key_path = self.data_dir / "cloud_public_key.txt"
        self.cloud_online = True
        self.last_ok = 0.0
        self.forwarded = 0
        self.gates: dict[str, set[WebSocket]] = {}
        self.upstream: dict[str, asyncio.Task] = {}
        self.kick = asyncio.Event()
        self.edge_id = os.getenv("TRUSTLANE_EDGE_ID", "edge-1")
        self.station_key = os.getenv("TRUSTLANE_STATION_KEY", "")
        self.last_report = 0.0
        self.gate_seen: dict[str, float] = {}

    async def report(self) -> None:
        """Tell the cloud this edge is alive and how much it is holding (shown in the ops console)."""
        body = {"edge_id": self.edge_id, "outbox": self.outbox.size(), "offline_exits": self.exits.size(),
                "forwarded": self.forwarded, "cloud_ok_age_s": round(time.time() - self.last_ok, 1) if self.last_ok else None,
                "gate_screens": {g: len(s) for g, s in self.gates.items()}}
        await self.call("POST", "/api/edge/heartbeat", json=body,
                        headers={"X-Station-Key": self.station_key} if self.station_key else {})

    # -- cloud health
    def mark(self, ok: bool) -> None:
        if ok:
            self.last_ok = time.time()
        if ok != self.cloud_online:
            log.warning("Cloud is %s", "reachable again" if ok else "UNREACHABLE: holding events, gates check codes locally")
        self.cloud_online = ok

    async def call(self, method: str, path: str, **kw) -> httpx.Response | None:
        """None when the cloud can't be reached (network error or 5xx)."""
        try:
            r = await self.client.request(method, path, **kw)
        except httpx.HTTPError:
            self.mark(False)
            return None
        if r.status_code >= 500:
            self.mark(False)
            return None
        self.mark(True)
        return r

    # -- local gate screens
    async def publish_gate(self, gate: str, msg: dict) -> None:
        for ws in list(self.gates.get(gate, ())):
            try:
                await ws.send_json(msg)
            except Exception:
                self.gates[gate].discard(ws)

    def public_key(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        if not self.key_path.exists():
            return None
        raw = self.key_path.read_text().strip()
        return Ed25519PublicKey.from_public_bytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))

    async def refresh_key(self) -> None:
        r = await self.call("GET", "/api/gate/public-key")
        if r is not None and r.status_code == 200:
            self.key_path.write_text(r.json()["key"])

    # -- forwarding
    async def flush(self) -> int:
        """Send queued station messages and offline exits, oldest first. Stops at the first outage."""
        sent = 0
        for row_id, path, body, headers, received in self.outbox.head():
            payload = json.loads(body)
            if path == STATION_KINDS["events"] and time.time() - received > 2:
                payload["received_ts"] = received  # held during an outage: match at the time the store saw it
            r = await self.call("POST", path, json=payload, headers=json.loads(headers))
            if r is None:
                return sent
            if r.status_code >= 400 and r.status_code != 429:
                log.warning("Cloud refused a queued %s (%s): dropped", path, r.text[:200])
            elif r.status_code == 429:
                return sent
            self.outbox.done(row_id)
            sent += 1
            self.forwarded += 1
        for row_id, path, body, headers, _ in self.exits.head():
            r = await self.call("POST", path, json=json.loads(body), headers=json.loads(headers))
            if r is None:
                return sent
            self.exits.done(row_id)
            sent += 1
        return sent


def create_edge_app(cloud: str, data_dir: Path | str, gate_key: str = "", client: httpx.AsyncClient | None = None,
                    connect_upstream: bool = True) -> FastAPI:
    state: EdgeState | None = None

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal state
        state = EdgeState(cloud, Path(data_dir), gate_key, client)
        app.state.edge = state
        await state.refresh_key()

        async def forwarder():
            backoff = 1.0
            while True:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(state.kick.wait(), timeout=backoff)
                state.kick.clear()
                await state.flush()
                if not state.cloud_online:
                    await state.call("GET", "/api/gate/status")  # probe
                backoff = 1.0 if state.cloud_online else min(backoff * 2, 30)
                if state.cloud_online and int(time.time()) % 300 < 2:
                    await state.refresh_key()
                if time.time() - state.last_report >= 30:
                    state.last_report = time.time()
                    await state.report()

        task = asyncio.create_task(forwarder())
        yield
        task.cancel()
        for t in state.upstream.values():
            t.cancel()

    app = FastAPI(title="TrustLane store edge", lifespan=lifespan)

    def check_gate(key: str | None) -> None:
        if gate_key and key != gate_key:
            raise HTTPException(401, "Gate key is missing or wrong")

    # ---------------------------------------------------------------- shelf stations: durable store-and-forward
    async def accept(kind: str, request: Request, x_station_key: str | None) -> dict:
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "Expected a JSON object")
        headers = {"X-Station-Key": x_station_key} if x_station_key else {}
        state.outbox.add(STATION_KINDS[kind], body, headers, time.time())
        state.kick.set()
        return {"ok": True, "queued": state.outbox.size()}

    @app.post("/api/shelf/events")
    async def events(request: Request, x_station_key: str | None = Header(default=None)):
        return await accept("events", request, x_station_key)

    @app.post("/api/shelf/heartbeat")
    async def heartbeat(request: Request, x_station_key: str | None = Header(default=None)):
        return await accept("heartbeat", request, x_station_key)

    @app.post("/api/shelf/presence")
    async def presence(request: Request, x_station_key: str | None = Header(default=None)):
        return await accept("presence", request, x_station_key)

    @app.post("/api/vision/audit")
    async def audit(request: Request, x_station_key: str | None = Header(default=None)):
        return await accept("audit", request, x_station_key)

    @app.get("/api/shelf/beacon")
    async def shelf_beacon(shelf_id: str, x_station_key: str | None = Header(default=None)):
        r = await state.call("GET", "/api/shelf/beacon", params={"shelf_id": shelf_id},
                             headers={"X-Station-Key": x_station_key} if x_station_key else {})
        if r is None:
            raise HTTPException(503, "Cloud unreachable: the station keeps using its cached shelf key")
        return JSONResponse(r.json(), status_code=r.status_code)

    # ---------------------------------------------------------------- exit gates
    @app.get("/api/gate/public-key")
    async def public_key():
        if not state.key_path.exists():
            await state.refresh_key()
        if not state.key_path.exists():
            raise HTTPException(503, "Gate key not cached yet and the cloud is unreachable")
        return {"alg": "Ed25519", "key": state.key_path.read_text().strip()}

    @app.get("/api/gate/status")
    async def gate_status():
        return {"cloud_online": state.cloud_online, "gate_key_required": bool(gate_key), "edge": True,
                "outbox": state.outbox.size(), "offline_exits": state.exits.size()}

    @app.post("/api/gate/{gate_id}/tap")
    async def tap(gate_id: str, request: Request, x_gate_key: str | None = Header(default=None)):
        check_gate(x_gate_key)
        body = await request.json()
        headers = {"X-Gate-Key": x_gate_key} if x_gate_key else {}
        r = await state.call("POST", f"/api/gate/{gate_id}/tap", json=body, headers=headers)
        if r is not None:
            return JSONResponse(r.json(), status_code=r.status_code)
        return await offline_tap(gate_id, body.get("token", ""), headers)

    async def offline_tap(gate_id: str, token: str, headers: dict) -> dict:
        key = state.public_key()
        if key is None:
            await state.publish_gate(gate_id, {"kind": "gate", "data": {"light": "red", "text": "Store offline: please see a team member"}})
            raise HTTPException(503, "Cloud unreachable and no cached gate key")
        try:
            payload = verify_token(token, key, time.time())
        except TokenError as err:
            await state.publish_gate(gate_id, {"kind": "gate", "data": {"light": "red", "text": f"Token rejected: {err}"}})
            raise HTTPException(401, str(err)) from err
        if payload["decision"] != "green":
            await state.publish_gate(gate_id, {"kind": "gate", "data": {"light": "red", "text": "Offline: a team member will help you"}})
            return {"mode": "edge-offline", "decision": payload["decision"]}
        state.exits.add(f"/api/gate/{gate_id}/offline-sync", {"token": token}, headers, time.time())
        await state.publish_gate(gate_id, {"kind": "gate", "data": {"light": "green", "text": "Offline check passed at the store. Gate open"}})
        return {"mode": "edge-offline", "decision": "green"}

    @app.post("/api/gate/{gate_id}/scale")
    async def scale(gate_id: str, request: Request, x_gate_key: str | None = Header(default=None)):
        check_gate(x_gate_key)
        r = await state.call("POST", f"/api/gate/{gate_id}/scale", json=await request.json(),
                             headers={"X-Gate-Key": x_gate_key} if x_gate_key else {})
        return {"ok": r is not None}

    @app.post("/api/gate/{gate_id}/offline-sync")
    async def offline_sync(gate_id: str, request: Request, x_gate_key: str | None = Header(default=None)):
        """A gate that couldn't reach even the edge: hold its exits here until the cloud takes them."""
        check_gate(x_gate_key)
        state.exits.add(f"/api/gate/{gate_id}/offline-sync", await request.json(),
                        {"X-Gate-Key": x_gate_key} if x_gate_key else {}, time.time())
        state.kick.set()
        return {"queued": True}

    @app.post("/api/gate/{gate_id}/heartbeat")
    async def gate_heartbeat(gate_id: str, request: Request, x_gate_key: str | None = Header(default=None)):
        check_gate(x_gate_key)
        state.gate_seen[gate_id] = time.time()
        await state.call("POST", f"/api/gate/{gate_id}/heartbeat", json=await request.json(),
                         headers={"X-Gate-Key": x_gate_key} if x_gate_key else {})
        return {"ok": True}

    @app.get("/metrics", include_in_schema=False)
    def edge_metrics():
        """Prometheus metrics for this edge (its own registry: nothing shared with a cloud in the same process)."""
        from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Gauge, generate_latest
        from fastapi.responses import Response
        reg = CollectorRegistry()
        Gauge("trustlane_edge_outbox", "Shelf messages waiting to reach the cloud", registry=reg).set(state.outbox.size())
        Gauge("trustlane_edge_offline_exits", "Exits let through offline, not yet billed", registry=reg).set(state.exits.size())
        Gauge("trustlane_edge_cloud_online", "1 if the cloud answered the last call", registry=reg).set(int(state.cloud_online))
        Gauge("trustlane_edge_forwarded", "Shelf messages forwarded since start", registry=reg).set(state.forwarded)
        Gauge("trustlane_edge_last_cloud_ok_seconds", "Unix time the cloud last answered", registry=reg).set(state.last_ok)
        g = Gauge("trustlane_edge_gate_last_seen_seconds", "Unix time a gate agent last reported here", ["gate"], registry=reg)
        for gate, ts in state.gate_seen.items():
            g.labels(gate).set(ts)
        return Response(generate_latest(reg), media_type=CONTENT_TYPE_LATEST)

    @app.get("/edge/status")
    async def status():
        return {"cloud": state.cloud, "cloud_online": state.cloud_online, "last_cloud_ok": state.last_ok,
                "outbox": state.outbox.size(), "offline_exits": state.exits.size(), "forwarded": state.forwarded,
                "gate_screens": {g: len(s) for g, s in state.gates.items()}}

    # ---------------------------------------------------------------- gate live channel (relay from the cloud)
    async def relay(gate: str) -> None:
        import websockets
        url = state.cloud.replace("http", "ws", 1) + f"/ws/gate:{gate}"
        while state.gates.get(gate):
            try:
                async with websockets.connect(url) as up:
                    async for raw in up:
                        await state.publish_gate(gate, json.loads(raw))
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(2)
        state.upstream.pop(gate, None)

    @app.websocket("/ws/{channel}")
    async def ws(channel: str, socket: WebSocket):
        if not channel.startswith("gate:"):
            await socket.close(code=4404)  # phones and the ops console use the cloud directly
            return
        gate = channel.split(":", 1)[1]
        await socket.accept()
        state.gates.setdefault(gate, set()).add(socket)
        if connect_upstream and gate not in state.upstream:
            state.upstream[gate] = asyncio.create_task(relay(gate))
        try:
            while True:
                await socket.receive_text()
        except WebSocketDisconnect:
            state.gates[gate].discard(socket)

    return app


def main() -> None:
    import uvicorn

    ap = argparse.ArgumentParser(description="TrustLane store edge gateway")
    ap.add_argument("--cloud", required=True, help="cloud server URL, e.g. https://trustlane.example")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8100)
    ap.add_argument("--data", default=str(Path(__file__).resolve().parent.parent / "data" / "edge"))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app = create_edge_app(args.cloud, args.data, os.getenv("TRUSTLANE_GATE_KEY", ""))
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
