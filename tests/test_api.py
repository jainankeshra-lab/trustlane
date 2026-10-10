"""End-to-end over HTTP: the same calls the phone, shelf and gate pages make."""
import pytest
from fastapi.testclient import TestClient

from server.config import Settings
from server.main import create_app
from tests.helpers import Phone, shelf, staff

RAZOR, MILK = "8901000000011", "8901000000066"


def make_settings(tmp_path, **overrides):
    settings = Settings()
    settings.data_dir = tmp_path
    settings.payments_provider = "sandbox"  # never call a real gateway from tests, even if .env has keys
    settings.staff_pin = "4321"
    for k, v in overrides.items():
        setattr(settings, k, v)
    return settings


@pytest.fixture
def app(tmp_path):
    return create_app(make_settings(tmp_path))


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


def test_honest_trip_over_http_with_websocket_push(client, app):
    p = Phone(client, aisle="7")
    from server.security import sign_request
    import time
    auth = sign_request(p.key, "WS", f"/ws/phone:{p.sid}", int(time.time() * 1000))
    with client.websocket_connect(f"/ws/phone:{p.sid}?auth={auth}") as ws:
        assert shelf(client).status_code == 200
        r = p.scan(RAZOR)
        assert r.status_code == 200 and r.json()["line"]["status"] == "verified"
        kinds = {ws.receive_json()["kind"] for _ in range(2)}
        assert "cart" in kinds
    assert p.exit().json()["mode"] == "online"
    cart = p.cart()
    assert cart["state"] == "exited" and cart["receipt"]["total"] == 29900
    summary = client.get("/api/ops/summary", headers=staff(app)).json()
    assert summary["exits"]["green"] == 1 and summary["stations"][0]["station_id"] == "st-S7-A"
    assert client.get("/api/ops/ledger/verify", headers=staff(app)).json()["intact"] is True
    assert "trustlane_exit_decisions_total" in client.get("/metrics").text


def test_checkout_returns_a_scannable_exit_qr(client):
    p = Phone(client, aisle="3")
    p.scan(MILK)
    r = p.post("/checkout").json()
    assert r["token"].count(".") == 1 and r["qr_svg"].startswith("<svg")


def test_offline_gateway_buffers_then_replays_and_charges(client, app):
    p = Phone(client, aisle="3")
    client.post("/api/ops/network", json={"online": False}, headers=staff(app))
    assert p.scan(MILK).status_code == 202          # buffered at the gateway
    r = p.exit()
    assert r.json() == {"mode": "offline", "decision": "green"}
    back = client.post("/api/ops/network", json={"online": True}, headers=staff(app)).json()
    assert back["replayed"] == 1
    cart = p.cart()
    assert cart["state"] == "exited" and cart["receipt"]["how"] == "offline"


def test_forged_token_is_refused_at_the_gate(client):
    assert client.post("/api/gate/G1/tap", json={"token": "e30.AAAA"}).status_code == 401


def test_station_key_is_enforced_when_configured(tmp_path):
    with TestClient(create_app(make_settings(tmp_path, station_key="secret"))) as c:
        assert shelf(c).status_code == 401
        ok = c.post("/api/shelf/heartbeat", json={"station_id": "a", "shelf_id": "S7-A"},
                    headers={"X-Station-Key": "secret"})
        assert ok.status_code == 200


def test_trip_needs_consent(client):
    assert client.post("/api/sessions", json={}).status_code == 400
