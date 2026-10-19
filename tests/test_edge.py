"""The store edge as its own process: durable store-and-forward of shelf messages, offline gate
decisions with the cached key, and catching up with the cloud after an outage."""
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from server.edge import create_edge_app
from server.main import create_app
from tests.helpers import Phone
from tests.test_api import make_settings

RAZOR = "8901000000011"


def down_client():
    def fail(request):
        raise httpx.ConnectError("cloud unreachable")
    return httpx.AsyncClient(base_url="http://cloud", transport=httpx.MockTransport(fail))


def up_client(cloud_app):
    return httpx.AsyncClient(base_url="http://cloud", transport=httpx.ASGITransport(app=cloud_app))


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def stack(tmp_path):
    cloud_app = create_app(make_settings(tmp_path / "cloud"))
    with TestClient(cloud_app) as cloud:
        edge_app = create_edge_app("http://cloud", tmp_path / "edge", client=up_client(cloud_app), connect_upstream=False)
        with TestClient(edge_app) as edge:
            yield cloud, edge, edge_app.state.edge


def take(edge, lane=0, shelf_id="S7-A"):
    return edge.post("/api/shelf/events", json={"station_id": "st-1", "shelf_id": shelf_id, "lane": lane,
                                               "kind": "take", "ts": time.time()})


def test_shelf_events_go_through_the_edge_to_the_cloud(stack):
    cloud, edge, state = stack
    p = Phone(cloud, aisle="7")
    assert take(edge).json()["ok"]
    assert wait_for(lambda: state.outbox.size() == 0)
    picks = cloud.app.state.engine.ledger._db.execute(
        "SELECT COUNT(*) FROM events WHERE kind='pick' AND session_id=?", (p.sid,)).fetchone()[0]
    assert picks == 1
    assert edge.get("/edge/status").json()["forwarded"] >= 1


def test_cloud_outage_holds_events_and_gates_decide_locally(stack):
    cloud, edge, state = stack
    p = Phone(cloud, aisle="7")
    p.scan(RAZOR)
    token = p.post("/checkout").json()["token"]
    cloud_client = state.client

    state.client = down_client()  # the store loses its internet link
    assert take(edge).status_code == 200  # still accepted: it waits on disk
    assert wait_for(lambda: not state.cloud_online)
    assert state.outbox.size() >= 1

    with edge.websocket_connect("/ws/gate:G1") as gate:
        r = edge.post("/api/gate/G1/tap", json={"token": token})
        assert r.json() == {"mode": "edge-offline", "decision": "green"}
        assert gate.receive_json()["data"]["light"] == "green"
    assert edge.post("/api/gate/G1/tap", json={"token": "forged.token"}).status_code == 401
    assert edge.get("/api/gate/status").json()["offline_exits"] == 1

    state.client = cloud_client  # link restored
    state.kick.set()
    assert wait_for(lambda: state.outbox.size() == 0 and state.exits.size() == 0)
    assert state.cloud_online
    assert p.cart()["state"] == "exited"  # billed through offline-sync


def test_red_tokens_never_pass_offline(stack):
    cloud, edge, state = stack
    p = Phone(cloud, aisle="7")
    from tests.helpers import shelf
    shelf(cloud, "take", lane=1)  # blades taken, never scanned
    cloud.app.state.engine.tick(time.time() + 120)
    token = p.post("/checkout").json()["token"]
    state.client = down_client()
    with edge.websocket_connect("/ws/gate:G1") as gate:
        r = edge.post("/api/gate/G1/tap", json={"token": token})
        assert r.json()["decision"] != "green"
        assert gate.receive_json()["data"]["light"] == "red"
    assert state.exits.size() == 0


def test_edge_enforces_the_gate_key(tmp_path):
    cloud_app = create_app(make_settings(tmp_path / "cloud", gate_key="gk"))
    with TestClient(cloud_app) as cloud:
        edge_app = create_edge_app("http://cloud", tmp_path / "edge", gate_key="gk", client=up_client(cloud_app),
                                   connect_upstream=False)
        with TestClient(edge_app) as edge:
            p = Phone(cloud)
            p.scan(RAZOR)
            token = p.post("/checkout").json()["token"]
            assert edge.post("/api/gate/G1/tap", json={"token": token}).status_code == 401
            assert edge.post("/api/gate/G1/tap", json={"token": token}, headers={"X-Gate-Key": "gk"}).status_code == 200


def test_outbox_survives_an_edge_restart(tmp_path):
    cloud_app = create_app(make_settings(tmp_path / "cloud"))
    with TestClient(cloud_app) as cloud:
        edge_app = create_edge_app("http://cloud", tmp_path / "edge", client=down_client(), connect_upstream=False)
        with TestClient(edge_app) as edge:
            take(edge)
            take(edge, lane=1)
        # edge process restarts with the link back
        edge_app = create_edge_app("http://cloud", tmp_path / "edge", client=up_client(cloud_app), connect_upstream=False)
        with TestClient(edge_app) as edge:
            state = edge_app.state.edge
            assert wait_for(lambda: state.outbox.size() == 0)
            assert state.forwarded == 2


def test_edge_reports_to_the_cloud_and_exposes_metrics(stack):
    cloud, edge, state = stack
    edge.portal.call(state.report)  # normally every 30 s from the forwarder loop
    summary = cloud.get("/api/ops/summary", headers={"X-Staff-Pin": "4321"}).json()
    assert [e["edge_id"] for e in summary["edges"]] == ["edge-1"] and summary["edges"][0]["ok"]
    edge.post("/api/gate/G1/heartbeat", json={"driver": "serial", "scale_g": 120.0, "camera": True, "offline_queue": 0})
    m = edge.get("/metrics").text
    assert "trustlane_edge_outbox 0.0" in m and "trustlane_edge_cloud_online 1.0" in m
    assert 'trustlane_edge_gate_last_seen_seconds{gate="G1"}' in m
    gates = cloud.get("/api/ops/summary", headers={"X-Staff-Pin": "4321"}).json()["gates"]
    assert gates[0]["gate_id"] == "G1" and gates[0]["scale_g"] == 120.0


def test_silent_gate_and_edge_raise_alerts(tmp_path):
    from server.catalog import Catalog
    from server.config import ROOT, Settings
    from server.engine import Engine
    from server.ledger import Ledger
    from server.payments import SandboxPayments
    from server.security import TokenSigner
    s = Settings()
    s.data_dir = tmp_path
    eng = Engine(s, Catalog.load(ROOT / "data" / "catalog.json"), Ledger(":memory:"), TokenSigner(tmp_path / "k.pem"),
                 SandboxPayments())
    eng.gate_heartbeat("G1", {"camera": False, "offline_queue": 2}, 0)
    eng.edge_heartbeat("edge-1", {"outbox": 500}, 0)
    ids = {a["id"] for a in eng.summary(10, True)["alerts"]}
    assert {"gate_camera:G1", "gate_queue:G1", "edge_backlog:edge-1"} <= ids
    ids = {a["id"] for a in eng.summary(500, True)["alerts"]}
    assert {"gate:G1", "edge:edge-1"} <= ids
