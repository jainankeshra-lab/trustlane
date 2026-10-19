"""Shelf tokens: shelves broadcast a signed rotating token, phones report what they hear (works for
iPhones in the background, where the phone can't broadcast its own ID)."""
import time

from fastapi.testclient import TestClient

from server.beacons import token_for
from server.main import create_app
from shelf_station.station import beacon_frame, beacon_token
from tests.helpers import Phone, shelf
from tests.test_api import make_settings


def beacon(c, shelf_id="S7-A", key=None):
    r = c.get("/api/shelf/beacon", params={"shelf_id": shelf_id}, headers={"X-Station-Key": key} if key else {})
    assert r.status_code == 200, r.text
    return r.json()


def heard(cfg, now=None, token=None):
    now = now or time.time()
    return {"shelf_no": cfg["shelf_no"], "token": token or token_for(bytes.fromhex(cfg["key"]), now), "rssi": -60}


def test_station_and_server_compute_the_same_token():
    key = bytes(range(16))
    for t in (0, 29.9, 30, 1_792_000_000):
        assert beacon_token(key, t) == token_for(key, t)
    frame = beacon_frame({"shelf_no": 7, "key": key.hex()}, 60)
    assert frame == f"A,0007{token_for(key, 60)}" and len(frame) == 14


def test_station_needs_its_key_for_the_shelf_secret(tmp_path):
    with TestClient(create_app(make_settings(tmp_path, station_key="sk"))) as c:
        assert c.get("/api/shelf/beacon", params={"shelf_id": "S7-A"}).status_code == 401
        cfg = beacon(c, key="sk")
        assert cfg["shelf_no"] >= 1 and len(cfg["key"]) == 32 and cfg["window_s"] == 30
        assert beacon(c, key="sk")["shelf_no"] == cfg["shelf_no"]  # stable
        assert c.get("/api/shelf/beacon", params={"shelf_id": "NOPE"}, headers={"X-Station-Key": "sk"}).status_code == 404


def test_a_heard_token_places_the_phone_at_that_shelf(tmp_path):
    with TestClient(create_app(make_settings(tmp_path))) as c:
        cfg = beacon(c)
        p = Phone(c)
        r = p.post("/heard", {"beacons": [heard(cfg)]}).json()
        assert r == {"verified": 1, "of": 1}
        sess = c.app.state.engine.sessions[p.sid]
        assert sess.presence_shelf == "S7-A" and sess.presence_aisle == "7"


def test_made_up_or_stale_tokens_do_not_count(tmp_path):
    with TestClient(create_app(make_settings(tmp_path))) as c:
        cfg = beacon(c)
        p = Phone(c)
        old = heard(cfg, now=time.time() - 120)
        fake = heard(cfg, token="deadbeef")
        unknown = {"shelf_no": 999, "token": "00000000", "rssi": -40}
        assert p.post("/heard", {"beacons": [old, fake, unknown, {"shelf_no": "x"}]}).json()["verified"] == 0
        assert c.app.state.engine.sessions[p.sid].presence_shelf is None


def test_phone_claiming_another_aisle_is_flagged(tmp_path):
    with TestClient(create_app(make_settings(tmp_path))) as c:
        cfg = beacon(c)
        p = Phone(c, aisle="3")
        c.app.state.engine.sessions[p.sid].aisle_since -= 30  # it has been claiming aisle 3 for a while
        p.post("/heard", {"beacons": [heard(cfg)]})
        reasons = c.app.state.engine.assess(p.sid)["reasons"]
        assert any("aisle 3" in str(r) for r in reasons)


def test_shelf_picks_go_to_the_phone_that_heard_the_shelf(tmp_path):
    with TestClient(create_app(make_settings(tmp_path))) as c:
        cfg = beacon(c)
        near, far = Phone(c), Phone(c, aisle="3")
        near.post("/heard", {"beacons": [heard(cfg)]})
        shelf(c, "take", lane=0)  # razor from S7-A
        led = c.app.state.engine.ledger._db
        rows = led.execute("SELECT session_id FROM events WHERE kind='pick'").fetchall()
        assert rows and rows[-1][0] == near.sid
