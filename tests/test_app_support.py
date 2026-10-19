"""Server features for the native app and operations: push notifications, offline gate sync,
SMS alerts, the camera model review queue, product images, the checkout page and the dev certificate."""
import asyncio
import base64
import json
import time

import httpx
from fastapi.testclient import TestClient

from server.alerts import AlertNotifier
from server.devcert import make_cert
from server.main import create_app
from server.push import PushSender
from tests.helpers import Phone, shelf, staff
from tests.test_api import make_settings

TOKEN = "ExponentPushToken[abc123]"
TINY_JPEG = base64.b64encode(b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9").decode()


def test_nudges_are_pushed_to_the_native_app(tmp_path):
    sent = []
    transport = httpx.MockTransport(lambda req: sent.extend(json.loads(req.content)) or httpx.Response(200, json={"data": []}))
    app = create_app(make_settings(tmp_path, match_window_s=0.5),
                     push_sender=PushSender(client=httpx.AsyncClient(transport=transport)))
    with TestClient(app) as c:
        p = Phone(c, aisle="7")
        assert p.post("/push-token", {"token": "not-a-token"}).status_code == 400
        assert p.post("/push-token", {"token": TOKEN}).status_code == 200
        shelf(c, lane=1)            # blades taken, not scanned
        deadline = time.time() + 5
        while not any(m["data"]["kind"] == "nudge" for m in sent) and time.time() < deadline:
            time.sleep(0.2)
    nudge = next(m for m in sent if m["data"]["kind"] == "nudge")
    assert nudge["to"] == TOKEN and "Razor blades" in nudge["body"] and nudge["priority"] == "high"


def test_gate_that_let_a_shopper_out_offline_syncs_the_bill_later(tmp_path):
    with TestClient(create_app(make_settings(tmp_path))) as c:
        p = Phone(c, aisle="3")
        p.scan("8901000000066")
        token = p.post("/checkout").json()["token"]
        # ... the gate lost the server, checked the token itself and opened. Back online:
        assert c.post("/api/gate/G1/offline-sync", json={"token": token}).json() == {"billed": True}
        assert p.cart()["state"] == "exited" and p.cart()["receipt"]["how"] == "offline"
        assert c.post("/api/gate/G1/offline-sync", json={"token": "e30.AAAA"}).status_code == 401


def test_serious_alerts_are_texted_to_on_call():
    calls = []
    transport = httpx.MockTransport(lambda req: calls.append(req) or httpx.Response(201))
    n = AlertNotifier(client=httpx.AsyncClient(transport=transport),
                      sms={"sid": "AC1", "token": "t", "from": "+10000000000", "to": ["+910000000001"]})
    asyncio.run(n.update([{"id": "latency", "level": "bad", "text": "slow"},
                          {"id": "match", "level": "warn", "text": "only Slack-worthy"}], 1))
    assert len(calls) == 1 and "Accounts/AC1/Messages.json" in str(calls[0].url)
    assert b"slow" in calls[0].content and b"To=%2B910000000001" in calls[0].content


def test_camera_snapshots_are_reviewed_into_the_training_set(tmp_path, monkeypatch):
    import server.main as main_mod
    monkeypatch.setattr(main_mod, "ROOT", tmp_path)  # keep the test's dataset out of the project
    app = create_app(make_settings(tmp_path / "data"))
    with TestClient(app) as c:
        body = {"station_id": "st", "shelf_id": "S7-A", "lane_sku": "razor", "camera_sku": "blades",
                "ts": time.time(), "jpeg_b64": TINY_JPEG}
        rid = c.post("/api/vision/audit", json=body).json()["id"]
        assert c.post("/api/vision/audit", json={**body, "jpeg_b64": base64.b64encode(b"GIF89a").decode()}).status_code == 400
        review = c.get("/api/vision/review", headers=staff(app)).json()
        assert review["pending"][0]["id"] == rid
        assert c.get(f"/api/vision/review/{rid}.jpg").status_code == 401
        assert c.get(f"/api/vision/review/{rid}.jpg", headers=staff(app)).content.startswith(b"\xff\xd8")
        assert c.post(f"/api/vision/review/{rid}", json={"label": "not-a-sku"}, headers=staff(app)).status_code == 400
        labelled = c.post(f"/api/vision/review/{rid}", json={"label": "blades"}, headers=staff(app)).json()
        assert labelled["status"] == "labelled"
        files = list((tmp_path / "datasets" / "shelf-products").rglob("*.jpg"))
        assert len(files) == 1 and files[0].parent.name == "blades"


def test_catalog_has_images_and_the_checkout_page_is_served(tmp_path):
    with TestClient(create_app(make_settings(tmp_path))) as c:
        razor = next(p for p in c.get("/api/catalog").json() if p["sku"] == "razor")
        assert razor["image"] == "/static/products/razor.svg"
        assert c.get(razor["image"]).text.startswith("<svg")
        assert "checkout.razorpay.com" in c.get("/checkout").text


def test_dev_certificate_covers_localhost(tmp_path):
    from cryptography import x509
    cert_path, key_path = make_cert(tmp_path)
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    assert "localhost" in names and key_path.exists()
