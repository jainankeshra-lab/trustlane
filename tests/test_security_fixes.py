"""The three exploits from the gap analysis, plus the access controls that close them."""
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from server.main import create_app
from server.security import eid_for, sign_request
from tests.helpers import Phone, shelf, staff
from tests.test_api import make_settings

RAZOR = "8901000000011"


@pytest.fixture
def app(tmp_path):
    return create_app(make_settings(tmp_path))


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------- exploit 1: silent location
def test_exploit1_silent_thief_is_no_longer_invisible(client):
    thief = Phone(client)            # never reports a location
    shelf(client, lane=0)            # pockets a razor
    thief.exit()
    cart = thief.cart()
    assert cart["state"] != "exited"  # was: exited, charged Rs 0
    assert cart["state"] in ("confirming", "staff_check")


def test_honest_located_shopper_is_not_pulled_in(client):
    honest = Phone(client, aisle="3")  # fresh location, elsewhere
    thief = Phone(client)
    shelf(client, lane=0)
    honest.exit()
    assert honest.cart()["state"] == "exited"
    thief.exit()
    assert thief.cart()["state"] != "exited"


def test_bluetooth_presence_beats_a_spoofed_aisle(client, app):
    spoofer = Phone(client, aisle="3")  # claims aisle 3 ...
    client.post("/api/shelf/presence",  # ... but the aisle-7 shelf hears its rotating Bluetooth ID
                json={"station_id": "st", "shelf_id": "S7-A", "eids": [eid_for(spoofer.key, time.time())]})
    shelf(client, lane=1)
    detail = client.get(f"/api/ops/trips/{spoofer.sid}", headers=staff(app)).json()
    assert detail["location"] == {**detail["location"], "reported": "3", "detected": "7"}
    reasons = " ".join(detail["assessment"]["reasons"])
    assert "Phone reported aisle 3" in reasons and "Razor blades" in reasons


# ---------------------------------------------------------------- exploit 2: self-clearing staff checks
def test_exploit2_shopper_cannot_clear_their_own_staff_check(client, app):
    thief = Phone(client, aisle="7")
    shelf(client, lane=1)
    thief.exit()  # amber: the phone shows the confirm screen
    picks = client.get(f"/api/ops/trips/{thief.sid}", headers=staff(app)).json()["picks"]
    thief.post("/confirm", {"take": [], "deny": [picks[0]["pick_id"]]})
    assert thief.cart()["state"] == "staff_check"
    r = client.post(f"/api/ops/checks/{thief.sid}/resolve", json={"outcome": "cleared"})
    assert r.status_code == 401
    r = client.post(f"/api/ops/checks/{thief.sid}/resolve", json={"outcome": "cleared"},
                    headers=thief.headers("POST", f"/api/ops/checks/{thief.sid}/resolve"))
    assert r.status_code == 401
    assert thief.cart()["state"] == "staff_check"
    for path in ("/api/ops/summary", "/api/ops/ledger/verify", "/api/erp/stock", "/api/ops/presence-sim"):
        assert client.get(path).status_code == 401
    assert client.post("/api/ops/network", json={"online": False}).status_code == 401


# ---------------------------------------------------------------- exploit 3: reading or driving other trips
def test_exploit3_other_trips_are_private(client):
    victim = Phone(client, aisle="3")
    attacker = Phone(client, aisle="3")
    assert not victim.sid.startswith("T0000")  # ids are random now
    assert client.get(f"/api/sessions/{victim.sid}").status_code == 401
    path = f"/api/sessions/{victim.sid}"
    assert client.get(path, headers=attacker.headers("GET", path)).status_code == 401  # wrong key
    for sub in ("/checkout", "/confirm", "/pay", "/resume", "/location"):
        assert client.post(f"/api/sessions/{victim.sid}{sub}", json={"aisle": "1"}).status_code == 401
    stale = sign_request(victim.key, "GET", path, int((time.time() - 3600) * 1000))
    assert client.get(path, headers={"X-Trip-Auth": stale}).status_code == 401  # replayed old signature


def test_websockets_need_credentials(client, app):
    p = Phone(client)
    for url in (f"/ws/phone:{p.sid}", f"/ws/phone:{p.sid}?auth=1.00", "/ws/ops", "/ws/ops?pin=0000"):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(url) as ws:
                ws.receive_json()
    with client.websocket_connect(f"/ws/ops?pin={app.state.staff_pin}"):
        pass


def test_borrowed_shopper_id_does_not_borrow_trust(client, app):
    loyal = Phone(client)
    app.state.engine.ledger.set_clean_streak(loyal.shopper["shopper_id"], 10)
    copycat = Phone(client, shopper={"shopper_id": loyal.shopper["shopper_id"]})  # no secret
    assert copycat.shopper["shopper_id"] != loyal.shopper["shopper_id"]
    again = Phone(client, shopper=loyal.shopper)  # the real phone, with its secret
    assert again.shopper == loyal.shopper


def test_rate_limit_on_starting_trips(tmp_path):
    with TestClient(create_app(make_settings(tmp_path, sessions_per_minute=3))) as c:
        codes = [c.post("/api/sessions", json={"consent": True}).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and 429 in codes[3:]
