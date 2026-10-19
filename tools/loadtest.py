"""Load test: many phones and shelf stations at once against a running TrustLane server, over HTTP.

Each simulated shopper starts a trip, reports aisles, scans 3-8 items (the shelf reports each take),
opens a live WebSocket, goes to the exit and taps the gate. Shoppers arrive over a ramp so the store
fills up gradually. The report gives latency percentiles per call, errors, and throughput.

Run the server without per-device rate limits first (one test machine looks like one very busy phone):

    TRUSTLANE_RATE_PER_SECOND=100000 TRUSTLANE_SESSIONS_PER_MINUTE=100000 TRUSTLANE_DATA_DIR=/tmp/tl-load \\
        uvicorn server.main:create_app --factory --port 8020
    python -m tools.loadtest --server http://127.0.0.1:8020 --shoppers 200 --ramp 30
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import time
from collections import defaultdict
from pathlib import Path

import httpx
import websockets

from server.config import ROOT
from server.security import sign_request, sign_scan

CATALOG = json.loads((ROOT / "data" / "catalog.json").read_text(encoding="utf-8"))["products"]
UNIT_ITEMS = [p for p in CATALOG if p.get("sold_by", "unit") == "unit" and not p.get("age_restricted")]


class Recorder:
    def __init__(self):
        self.lat: dict[str, list[float]] = defaultdict(list)
        self.errors: dict[str, int] = defaultdict(int)
        self.calls = 0

    async def call(self, name: str, client: httpx.AsyncClient, method: str, path: str, **kw) -> httpx.Response | None:
        t = time.perf_counter()
        try:
            r = await client.request(method, path, **kw)
        except httpx.HTTPError:
            self.errors[name] += 1
            return None
        self.lat[name].append(time.perf_counter() - t)
        self.calls += 1
        if r.status_code >= 400:
            self.errors[f"{name} {r.status_code}"] += 1
        return r


async def shopper(n: int, base: str, c: httpx.AsyncClient, rec: Recorder, rng: random.Random) -> None:
    if True:  # one shared connection pool: building a client per shopper would stall the load generator itself
        r = await rec.call("start trip", c, "POST", "/api/sessions", json={"consent": True})
        if r is None or r.status_code != 200:
            return
        sid, key = r.json()["session_id"], r.json()["key"]

        def auth(method: str, path: str) -> dict:
            return {"X-Trip-Auth": sign_request(key, method, path, int(time.time() * 1000))}

        ws_url = base.replace("http", "ws", 1) + f"/ws/phone:{sid}?auth=" + sign_request(key, "WS", f"/ws/phone:{sid}",
                                                                                         int(time.time() * 1000))
        async with websockets.connect(ws_url) as ws:
            seq = 0
            for _ in range(rng.randint(3, 8)):
                item = rng.choice(UNIT_ITEMS)
                path = f"/api/sessions/{sid}/location"
                await rec.call("location", c, "POST", path, json={"aisle": item["aisle"]}, headers=auth("POST", path))
                if item.get("shelf_id"):
                    await rec.call("shelf event", c, "POST", "/api/shelf/events", json={
                        "station_id": f"st-{item['shelf_id']}", "shelf_id": item["shelf_id"], "lane": item["lane"],
                        "kind": "take", "ts": time.time()})
                await asyncio.sleep(rng.uniform(0.2, 1.5))
                seq += 1
                ts = int(time.time() * 1000)
                await rec.call("scan", c, "POST", f"/api/sessions/{sid}/scans", json={
                    "seq": seq, "barcode": item["barcode"], "ts": ts, "sig": sign_scan(key, sid, seq, item["barcode"], ts)})
                await asyncio.sleep(rng.uniform(0.2, 1.0))
            path = f"/api/sessions/{sid}"
            await rec.call("cart", c, "GET", path, headers=auth("GET", path))
            path = f"/api/sessions/{sid}/checkout"
            r = await rec.call("checkout", c, "POST", path, headers=auth("POST", path))
            if r is not None and r.status_code == 200:
                await rec.call("gate tap", c, "POST", "/api/gate/G1/tap", json={"token": r.json()["token"]})
            try:
                while True:  # drain what the server pushed to this phone
                    await asyncio.wait_for(ws.recv(), timeout=0.05)
            except (asyncio.TimeoutError, websockets.ConnectionClosed):
                pass


def pct(values: list[float], q: float) -> float:
    values = sorted(values)
    return values[min(len(values) - 1, int(q * (len(values) - 1)))] * 1000


async def run(base: str, shoppers: int, ramp: float, seed: int) -> dict:
    rec = Recorder()
    rng = random.Random(seed)
    start = time.perf_counter()

    limits = httpx.Limits(max_connections=shoppers + 10, max_keepalive_connections=shoppers + 10)
    async with httpx.AsyncClient(base_url=base, timeout=60, limits=limits) as client:
        async def delayed(i: int):
            await asyncio.sleep(ramp * i / max(1, shoppers))
            await shopper(i, base, client, rec, random.Random(rng.random()))

        await asyncio.gather(*(delayed(i) for i in range(shoppers)))
    took = time.perf_counter() - start
    rows = {name: {"calls": len(v), "p50_ms": round(pct(v, 0.5), 1), "p95_ms": round(pct(v, 0.95), 1),
                   "p99_ms": round(pct(v, 0.99), 1), "max_ms": round(max(v) * 1000, 1)}
            for name, v in sorted(rec.lat.items())}
    all_lat = [x for v in rec.lat.values() for x in v]
    return {"shoppers": shoppers, "ramp_s": ramp, "seconds": round(took, 1), "calls": rec.calls,
            "calls_per_s": round(rec.calls / took, 1), "p95_ms_all": round(pct(all_lat, 0.95), 1) if all_lat else None,
            "errors": dict(rec.errors), "by_call": rows}


def to_markdown(results: list[dict]) -> str:
    lines = ["# TrustLane load test", "",
             "Simulated phones and shelf stations over HTTP and WebSockets against one server process "
             "(one store). Each shopper: start, 3-8 aisle reports, shelf takes and scans, cart, checkout, gate tap.", "",
             "| Shoppers (arriving over) | Requests | Requests/s | p95, all calls | Scan p95 | Shelf event p95 | Gate tap p95 | Errors |",
             "|---|---|---|---|---|---|---|---|"]
    for r in results:
        b = r["by_call"]
        lines.append(f"| {r['shoppers']} ({r['ramp_s']:.0f} s) | {r['calls']} | {r['calls_per_s']} | {r['p95_ms_all']} ms | "
                     f"{b.get('scan', {}).get('p95_ms', '–')} ms | {b.get('shelf event', {}).get('p95_ms', '–')} ms | "
                     f"{b.get('gate tap', {}).get('p95_ms', '–')} ms | {sum(r['errors'].values()) or 0} |")
    lines += ["", "The deck's alert threshold for shelf-to-server latency is a p95 of 2 s."]
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description="TrustLane load test")
    ap.add_argument("--server", default="http://127.0.0.1:8020")
    ap.add_argument("--shoppers", type=int, nargs="+", default=[50, 100, 200])
    ap.add_argument("--ramp", type=float, default=20, help="seconds over which shoppers arrive")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", default=str(ROOT / "reports"))
    args = ap.parse_args()
    results = []
    for n in args.shoppers:
        r = asyncio.run(run(args.server, n, args.ramp, args.seed))
        print(json.dumps({k: v for k, v in r.items() if k != "by_call"}))
        results.append(r)
    out = Path(args.out)
    out.mkdir(exist_ok=True)
    (out / "load.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    (out / "load.md").write_text(to_markdown(results), encoding="utf-8")
    print(to_markdown(results))


if __name__ == "__main__":
    main()
