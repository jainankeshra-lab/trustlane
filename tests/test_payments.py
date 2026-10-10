"""Razorpay test-mode integration against a fake Razorpay API (same endpoints and signature scheme)."""
import hashlib
import hmac
import itertools
import json
import re
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from server.catalog import Catalog
from server.config import ROOT, Settings
from server.engine import Engine, EngineError
from server.ledger import Ledger
from server.main import create_app
from server.payments import RazorpayPayments
from server.security import TokenSigner, sign_scan, verify_token
from tests.helpers import Phone

KEY_ID, SECRET, WEBHOOK_SECRET = "rzp_test_fakeKey123", "fake_secret_abc", "whsec_fake"
MILK, RAZOR = "8901000000066", "8901000000011"


class FakeRazorpay:
    def __init__(self):
        self.orders, self.payments, self.calls = {}, {}, []
        self.ids = itertools.count(1)
        self.down = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"].startswith("Basic ")
        path, self.calls = request.url.path, self.calls + [(request.method, request.url.path)]
        if self.down:
            return httpx.Response(503, json={"error": {"description": "service unavailable"}})
        if request.method == "POST" and path == "/v1/orders":
            body = json.loads(request.content)
            oid = f"order_{next(self.ids):04d}"
            self.orders[oid] = {"id": oid, "amount": body["amount"], "currency": "INR", "status": "created",
                                "receipt": body["receipt"], "notes": body["notes"]}
            return httpx.Response(200, json=self.orders[oid])
        if m := re.fullmatch(r"/v1/payments/(\w+)/capture", path):
            self.payments[m[1]]["status"] = "captured"
            return httpx.Response(200, json=self.payments[m[1]])
        if m := re.fullmatch(r"/v1/payments/(\w+)", path):
            return httpx.Response(200, json=self.payments[m[1]]) if m[1] in self.payments else \
                httpx.Response(400, json={"error": {"description": "The id provided does not exist"}})
        return httpx.Response(404)

    def pay(self, order_id, status="authorized", secret=SECRET):
        """What Checkout does when the shopper approves with success@razorpay."""
        pid = f"pay_{next(self.ids):04d}"
        self.payments[pid] = {"id": pid, "order_id": order_id, "amount": self.orders[order_id]["amount"],
                              "currency": "INR", "status": status, "method": "upi", "vpa": "success@razorpay"}
        sig = hmac.new(secret.encode(), f"{order_id}|{pid}".encode(), hashlib.sha256).hexdigest()
        return pid, sig


@pytest.fixture
def fake():
    return FakeRazorpay()


@pytest.fixture
def razorpay(fake):
    client = httpx.Client(base_url=RazorpayPayments.API, auth=(KEY_ID, SECRET), transport=httpx.MockTransport(fake.handler))
    return RazorpayPayments(KEY_ID, SECRET, WEBHOOK_SECRET, client=client)


@pytest.fixture
def engine(tmp_path, razorpay):
    return Engine(Settings(), Catalog.load(ROOT / "data" / "catalog.json"), Ledger(":memory:"),
                  TokenSigner(tmp_path / "k.pem"), razorpay)


def shop_and_exit(engine, barcodes=(MILK,)):
    sess, _, _ = engine.start_session(None, None, True, 0)
    for seq, bc in enumerate(barcodes, 1):
        engine.scan(sess.session_id, seq, bc, seq, sign_scan(sess.key, sess.session_id, seq, bc, seq), 1)
    token, _ = engine.checkout(sess.session_id, 2)
    notes = engine.gate_tap("G1", verify_token(token, engine.signer.public_key, 2), 2)
    return sess, notes


def test_live_keys_are_refused():
    with pytest.raises(ValueError, match="TEST keys"):
        RazorpayPayments("rzp_live_abc", "secret")


def test_green_exit_asks_the_phone_to_approve_then_captures(engine, fake):
    sess, notes = shop_and_exit(engine)
    pay = next(n.data for n in notes if n.kind == "pay")
    assert pay["key"] == KEY_ID and pay["amount"] == 6800 and "secret" not in json.dumps(pay)
    assert any(n.kind == "gate" and n.data["light"] == "amber" for n in notes)
    assert sess.state == "paying"

    pid, sig = fake.pay(pay["order_id"])
    done = engine.payment_confirmed(sess.session_id, pay["order_id"], pid, sig, 5)
    assert sess.state == "exited"
    assert sess.receipt["provider"] == "razorpay" and sess.receipt["vpa"] == "success@razorpay"
    assert ("POST", f"/v1/payments/{pid}/capture") in fake.calls  # authorized -> captured
    assert any(n.kind == "gate" and n.data["light"] == "green" for n in done)


def test_forged_signature_keeps_the_gate_closed(engine, fake):
    sess, notes = shop_and_exit(engine)
    order_id = next(n.data for n in notes if n.kind == "pay")["order_id"]
    pid, _ = fake.pay(order_id)
    _, forged = fake.pay(order_id, secret="not-the-secret")
    with pytest.raises(EngineError) as err:
        engine.payment_confirmed(sess.session_id, order_id, pid, forged, 5)
    assert err.value.status == 402 and sess.state == "paying"
    assert any(n.kind == "payment_failed" for n in err.value.notes)


def test_payment_from_another_order_is_rejected(engine, fake):
    sess, notes = shop_and_exit(engine)
    order_id = next(n.data for n in notes if n.kind == "pay")["order_id"]
    other = fake.handler(httpx.Request("POST", "https://api.razorpay.com/v1/orders",
                                       headers={"authorization": "Basic x"},
                                       content=json.dumps({"amount": 100, "receipt": "x", "notes": {}}))).json()["id"]
    pid, _ = fake.pay(other)
    sig = hmac.new(SECRET.encode(), f"{order_id}|{pid}".encode(), hashlib.sha256).hexdigest()
    with pytest.raises(EngineError):
        engine.payment_confirmed(sess.session_id, order_id, pid, sig, 5)


def test_retry_reuses_the_order_and_gateway_outage_is_recoverable(engine, fake):
    fake.down = True
    sess, notes = shop_and_exit(engine)
    assert any(n.kind == "payment_failed" for n in notes) and sess.state == "paying"
    fake.down = False
    first = next(n.data for n in engine.pay_in_app(sess.session_id, 3) if n.kind == "pay")["order_id"]
    again = next(n.data for n in engine.pay_in_app(sess.session_id, 4) if n.kind == "pay")["order_id"]
    assert first == again


def test_empty_cart_needs_no_payment(engine):
    sess, notes = shop_and_exit(engine, barcodes=())
    assert sess.state == "exited" and not any(n.kind == "pay" for n in notes)


def test_http_flow_with_webhook(tmp_path, razorpay, fake):
    settings = Settings()
    settings.data_dir = tmp_path
    settings.staff_pin = "1111"
    app = create_app(settings, payments=razorpay)
    with TestClient(app) as c:
        assert c.get("/api/config").json()["payments"] == {"provider": "razorpay", "flow": "checkout",
                                                            "mode": "test", "key_id": KEY_ID}
        p = Phone(c)
        p.scan(RAZOR)
        p.exit()
        assert p.cart()["state"] == "paying"

        order_id = next(iter(fake.orders))
        pid, _ = fake.pay(order_id, status="captured")
        event = json.dumps({"event": "payment.captured", "payload": {"payment": {"entity": fake.payments[pid]}}}).encode()
        bad = c.post("/api/payments/razorpay/webhook", content=event, headers={"X-Razorpay-Signature": "00"})
        assert bad.status_code == 400
        good_sig = hmac.new(WEBHOOK_SECRET.encode(), event, hashlib.sha256).hexdigest()
        assert c.post("/api/payments/razorpay/webhook", content=event,
                      headers={"X-Razorpay-Signature": good_sig}).status_code == 200
        cart = p.cart()
        assert cart["state"] == "exited" and cart["receipt"]["payment_id"] == pid
        summary = c.get("/api/ops/summary", headers={"X-Staff-Pin": "1111"}).json()
        assert summary["payments"]["provider"] == "razorpay" and summary["payments"]["ok"] == 1
