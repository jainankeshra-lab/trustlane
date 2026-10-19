"""Real exit-gate pieces: NTAG 424 DNA NFC taps, the gate device key, the floor scale feed, the gate
agent (camera codes, relay/light, offline fallback), and the on-phone package-check model."""
import base64
import io
import json
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from gate_agent.agent import ConsoleDriver, GateAgent
from server.main import create_app
from server.nfc import SunError, decrypt_sun, make_sun
from tests.helpers import Phone
from tests.test_api import make_settings

RAZOR, GUM = "8901000000011", "8901000000059"
META, FILE = "11" * 16, "22" * 16
UID = "04AABBCCDDEE01"


def nfc_setup(data_dir):
    (data_dir / "nfc_tags.json").write_text(json.dumps({"meta_key": META, "file_key": FILE, "tags": {UID: "G2"}}))


def tag_read(counter, uid=UID, meta=META, file=FILE):
    picc, mac = make_sun(uid, counter, bytes.fromhex(meta), bytes.fromhex(file))
    return {"picc_data": picc, "cmac": mac}


# ---------------------------------------------------------------- NFC (SUN messages)
def test_sun_matches_nxp_published_example():
    # NXP AN12196 / the SDM reference backend: all-zero keys, UID 04DE5F1EACC040, read counter 61.
    zero = bytes(16)
    assert decrypt_sun("EF963FF7828658A599F3041510671E88", "94EED9EE65337086", zero, zero) == ("04DE5F1EACC040", 61)
    with pytest.raises(SunError):
        decrypt_sun("EF963FF7828658A599F3041510671E88", "94EED9EE65337087", zero, zero)


def test_nfc_tap_opens_the_gate_the_tag_belongs_to(tmp_path):
    nfc_setup(tmp_path)
    with TestClient(create_app(make_settings(tmp_path))) as c:
        p = Phone(c)
        p.scan(RAZOR)
        token = p.post("/checkout").json()["token"]
        with c.websocket_connect("/ws/gate:G2") as gate:
            r = c.post("/api/gate/nfc-tap", json={"token": token, **tag_read(5)})
            assert r.status_code == 200, r.text
            assert r.json()["gate"] == "G2"  # from the tag's UID, not anything the phone said
            assert gate.receive_json()["data"]["light"] in ("green", "amber", "red")
        assert p.cart()["state"] in ("exited", "confirming", "staff")


def test_nfc_replay_forgery_and_unknown_tags_are_refused(tmp_path):
    nfc_setup(tmp_path)
    with TestClient(create_app(make_settings(tmp_path))) as c:
        p = Phone(c)
        p.scan(RAZOR)
        token = p.post("/checkout").json()["token"]
        read = tag_read(9)
        assert c.post("/api/gate/nfc-tap", json={"token": token, **read}).status_code == 200
        replay = c.post("/api/gate/nfc-tap", json={"token": token, **read})
        assert replay.status_code == 401 and "already used" in replay.json()["detail"]
        older = c.post("/api/gate/nfc-tap", json={"token": token, **tag_read(8)})
        assert older.status_code == 401
        forged = c.post("/api/gate/nfc-tap", json={"token": token, **tag_read(20, file="33" * 16)})
        assert forged.status_code == 401 and "genuine" in forged.json()["detail"]
        stranger = c.post("/api/gate/nfc-tap", json={"token": token, **tag_read(21, uid="04000000000099")})
        assert stranger.status_code == 401 and "registered" in stranger.json()["detail"]


def test_nfc_without_tags_configured(tmp_path):
    with TestClient(create_app(make_settings(tmp_path))) as c:
        assert c.get("/api/gate/status").json()["nfc"] is False
        r = c.post("/api/gate/nfc-tap", json={"token": "x.y", **tag_read(1)})
        assert r.status_code == 401


def test_tag_url_lands_on_the_phone_app(tmp_path):
    with TestClient(create_app(make_settings(tmp_path))) as c:
        r = c.get("/g?picc_data=AB&cmac=CD", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/phone?picc_data=AB&cmac=CD"


# ---------------------------------------------------------------- gate key and floor scale
def test_gate_key_locks_direct_taps_to_gate_devices(tmp_path):
    nfc_setup(tmp_path)
    with TestClient(create_app(make_settings(tmp_path, gate_key="gk-123"))) as c:
        assert c.get("/api/gate/status").json()["gate_key_required"] is True
        p = Phone(c)
        p.scan(RAZOR)
        token = p.post("/checkout").json()["token"]
        assert c.post("/api/gate/G1/tap", json={"token": token}).status_code == 401  # a phone can't just claim a gate
        assert c.post("/api/gate/G1/scale", json={"weight_g": 100}).status_code == 401
        assert c.post("/api/gate/G1/tap", json={"token": token}, headers={"X-Gate-Key": "gk-123"}).status_code == 200


def test_floor_scale_feed_is_used_for_the_next_tap(tmp_path):
    nfc_setup(tmp_path)
    with TestClient(create_app(make_settings(tmp_path))) as c:
        p = Phone(c)
        p.scan(GUM)  # about 30 g of gum scanned...
        c.post("/api/gate/G2/scale", json={"weight_g": 2500})  # ...but the basket on the floor scale weighs 2.5 kg
        token = p.post("/checkout").json()["token"]
        c.post("/api/gate/nfc-tap", json={"token": token, **tag_read(3)})
        cart = p.cart()
        assert cart["state"] != "exited"
        reasons = c.app.state.engine.assess(p.sid)["reasons"]
        assert any("Floor scale" in str(r) for r in reasons)


# ---------------------------------------------------------------- gate agent
def agent_for(client, tmp_path, **kw):
    driver = ConsoleDriver()
    return GateAgent("http://testserver", "G1", driver, client=client, state_dir=tmp_path / "gate", **kw), driver


def test_agent_reads_a_code_and_drives_relay_and_light(tmp_path):
    with TestClient(create_app(make_settings(tmp_path))) as c:
        agent, driver = agent_for(c, tmp_path)
        p = Phone(c)
        p.scan(GUM)
        token = p.post("/checkout").json()["token"]
        with c.websocket_connect("/ws/gate:G1") as ws:
            assert agent.submit(token) == "online"
            assert agent.submit(token) == "duplicate"  # still in front of the camera
            agent.on_message(ws.receive_json())
        assert ("light", "green") in driver.events and ("open", 3000) in driver.events
        assert agent.submit("garbage.code") == "rejected" and driver.events[-1] == ("light", "red")


def test_agent_uses_scale_reading_in_taps(tmp_path):
    with TestClient(create_app(make_settings(tmp_path))) as c:
        agent, driver = agent_for(c, tmp_path)
        driver.weight_g, driver.weight_ts = 4000.0, time.time()
        p = Phone(c)
        p.scan(GUM)
        agent.submit(p.post("/checkout").json()["token"])
        assert p.cart()["state"] != "exited"


def test_agent_opens_offline_for_green_codes_and_syncs_later(tmp_path):
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as c:
        p = Phone(c)
        p.scan(GUM)
        token = p.post("/checkout").json()["token"]
        agent, driver = agent_for(c, tmp_path)
        agent.refresh_key()

        class Down:  # the server is unreachable
            def post(self, *a, **k):
                import httpx
                raise httpx.ConnectError("down")
        agent.client = Down()
        assert agent.submit(token) == "offline-open"
        assert driver.events[-2:] == [("light", "green"), ("open", 3000)]
        assert agent.submit("x.y") == "rejected"

        agent.client = c  # back online
        assert agent.sync_offline() == 1
        assert p.cart()["state"] == "exited"
        assert agent.sync_offline() == 0


# ---------------------------------------------------------------- package check model
def tiny_model(path):
    """3-class stand-in for the shelf classifier: the dominant colour picks the class."""
    import onnx
    from onnx import TensorProto, helper

    nodes = [
        helper.make_node("ReduceMean", ["images"], ["m"], axes=[2, 3], keepdims=0),
        helper.make_node("Mul", ["m", "k"], ["s"]),
        helper.make_node("Softmax", ["s"], ["output0"], axis=1),
    ]
    graph = helper.make_graph(nodes, "tiny", [helper.make_tensor_value_info("images", TensorProto.FLOAT, [1, 3, 8, 8])],
                              [helper.make_tensor_value_info("output0", TensorProto.FLOAT, [1, 3])],
                              [helper.make_tensor("k", TensorProto.FLOAT, [], [10.0])])
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 8
    model.metadata_props.add(key="names", value="{0: 'razor', 1: 'gum', 2: 'empty'}")
    onnx.save(model, str(path))


def jpeg_b64(color):
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), color).save(buf, "JPEG")
    return base64.b64encode(buf.getvalue()).decode()


def test_package_model_is_served_and_checks_photos(tmp_path):
    model = tmp_path / "pkg.onnx"
    tiny_model(model)
    with TestClient(create_app(make_settings(tmp_path, package_model_path=model))) as c:
        info = c.get("/api/vision/package-model").json()
        assert info["available"] and info["names"] == ["razor", "gum", "empty"] and info["input"]["size"] == 8
        assert c.get(info["url"]).content == model.read_bytes()
        p = Phone(c)
        red = p.post("/package-check", {"image_b64": jpeg_b64((230, 20, 20))}).json()
        assert red["label"] == "razor" and red["confidence"] > 0.9 and red["where"] == "server"
        assert p.post("/package-check", {"image_b64": "not base64!"}).status_code == 400
        assert c.post(f"/api/sessions/{p.sid}/package-check", json={"image_b64": jpeg_b64((0, 0, 0))}).status_code == 401


def test_package_check_without_a_model(tmp_path):
    with TestClient(create_app(make_settings(tmp_path))) as c:
        assert c.get("/api/vision/package-model").json() == {"available": False}
        p = Phone(c)
        assert p.post("/package-check", {"image_b64": jpeg_b64((1, 2, 3))}).status_code == 503


def test_empty_or_unknown_package_labels_never_flag_a_scan(tmp_path):
    with TestClient(create_app(make_settings(tmp_path))) as c:
        p = Phone(c)
        p.seq += 1
        from server.security import sign_scan
        ts = int(time.time() * 1000)
        r = c.post(f"/api/sessions/{p.sid}/scans", json={
            "seq": p.seq, "barcode": GUM, "ts": ts, "sig": sign_scan(p.key, p.sid, p.seq, GUM, ts),
            "package_label": "empty", "package_confidence": 0.99})
        assert r.json()["line"]["status"] != "flagged"


def test_agent_heartbeat_reaches_the_ops_console(tmp_path):
    with TestClient(create_app(make_settings(tmp_path))) as c:
        agent, driver = agent_for(c, tmp_path)
        driver.weight_g, driver.weight_ts = 640.0, time.time()
        agent.heartbeat(camera_ok=True)
        gates = c.get("/api/ops/summary", headers={"X-Staff-Pin": "4321"}).json()["gates"]
        assert gates[0]["gate_id"] == "G1" and gates[0]["driver"] == "console" and gates[0]["scale_g"] == 640.0
        assert "trustlane_gate_last_seen_seconds" in c.get("/metrics").text
