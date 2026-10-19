"""UPI Reserve Pay through Cashfree (sandbox) against a fake Cashfree API with the same endpoints,
headers and webhook signature scheme."""
import base64
import hashlib
import hmac
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from server.catalog import Catalog
from server.config import ROOT, Settings
from server.engine import Engine, EngineError
from server.ledger import Ledger
from server.main import create_app
from server.payments import CashfreeReservePay, payments_from_settings
from server.security import TokenSigner, sign_scan, verify_token
from tests.helpers import Phone

CLIENT_ID, SECRET = "TEST_cf_client", "cfsk_test_secret"
MILK = "8901000000066"


class FakeCashfree:
    def __init__(self):
        self.subs, self.pays, self.calls, self.refunds = {}, [], [], []
        self.decline = False
        self.refund_status = "PENDING"

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["x-client-id"] == CLIENT_ID and request.headers["x-api-version"] == "2025-01-01"
        path = request.url.path.removeprefix("/pg")
        body = json.loads(request.content) if request.content else {}
        self.calls.append((request.method, path, body))
        if path == "/subscriptions":
            assert body["subscription_meta"]["category"] == "SBMD" and body["plan_details"]["plan_type"] == "ON_DEMAND"
            self.subs[body["subscription_id"]] = "INITIALIZED"
            return httpx.Response(200, json={"subscription_id": body["subscription_id"], "subscription_status": "INITIALIZED"})
        if path == "/subscriptions/pay":
            if body["payment_type"] == "CHARGE" and self.decline:
                return httpx.Response(400, json={"message": "insufficient blocked amount"})
            self.pays.append(body)
            data = {"url": "https://sandbox.cashfree.com/approve/x"} if body["payment_type"] == "AUTH" else {}
            return httpx.Response(200, json={"payment_id": body["payment_id"], "cf_payment_id": "123",
                                             "payment_status": "PENDING", "data": data})
        if path.startswith("/subscriptions/") and path.endswith("/refunds"):
            assert request.headers["x-idempotency-key"] == body["refund_id"]
            assert path == f"/subscriptions/{body['subscription_id']}/refunds"
            self.refunds.append(body)
            return httpx.Response(200, json={"refund_id": body["refund_id"], "cf_refund_id": "cfr1",
                                             "refund_amount": body["refund_amount"], "refund_status": self.refund_status})
        return httpx.Response(404, json={"message": "not found"})


def signed(event: dict, ts: str | None = None, secret: str = SECRET):
    raw = json.dumps(event).encode()
    ts = ts or str(int(time.time() * 1000))
    sig = base64.b64encode(hmac.new(secret.encode(), ts.encode() + raw, hashlib.sha256).digest()).decode()
    return raw, {"x-webhook-signature": sig, "x-webhook-timestamp": ts, "content-type": "application/json"}


def auth_event(sub_id, status="ACTIVE"):
    return {"type": "SUBSCRIPTION_AUTH_STATUS",
            "data": {"subscription_details": {"subscription_id": sub_id, "subscription_status": status}}}


@pytest.fixture
def fake():
    return FakeCashfree()


@pytest.fixture
def cashfree(fake):
    client = httpx.Client(base_url=CashfreeReservePay.API, transport=httpx.MockTransport(fake.handler),
                          headers={"x-client-id": CLIENT_ID, "x-client-secret": SECRET, "x-api-version": "2025-01-01"})
    return CashfreeReservePay(CLIENT_ID, SECRET, max_amount_paise=100_000, client=client)


@pytest.fixture
def engine(tmp_path, cashfree):
    return Engine(Settings(), Catalog.load(ROOT / "data" / "catalog.json"), Ledger(":memory:"),
                  TokenSigner(tmp_path / "k.pem"), cashfree)


def trip(engine, creds=None, barcodes=(MILK,), now=10):
    sess, new_creds, _ = engine.start_session(*(creds or (None, None)), True, now)
    for seq, bc in enumerate(barcodes, 1):
        engine.scan(sess.session_id, seq, bc, seq, sign_scan(sess.key, sess.session_id, seq, bc, seq), now + 1)
    token, _ = engine.checkout(sess.session_id, now + 2)
    notes = engine.gate_tap("G1", verify_token(token, engine.signer.public_key, now + 2), now + 2)
    return sess, new_creds or creds, notes


def test_settings_pick_cashfree_and_need_both_keys():
    s = Settings()
    s.payments_provider, s.cashfree_client_id, s.cashfree_client_secret = "auto", CLIENT_ID, SECRET
    assert payments_from_settings(s).flow == "mandate"
    s.cashfree_client_secret = ""
    with pytest.raises(ValueError):
        payments_from_settings(s)


def test_no_mandate_sends_the_shopper_to_the_desk(engine, fake):
    sess, _, notes = trip(engine)
    assert sess.state == "confirming" and not fake.pays
    assert any(n.kind == "payment_failed" and "staff desk" in n.data["text"] for n in notes)


def test_active_mandate_is_debited_at_the_gate_with_no_tap(engine, fake):
    first, creds, _ = trip(engine)  # first visit registers the shopper
    m, _ = engine.link_mandate(*creds, {"name": "Asha"}, "success@upi", 20)
    assert fake.pays[-1]["payment_type"] == "AUTH" and fake.pays[-1]["payment_method"]["upi"]["channel"] == "collect"
    assert engine.mandate_status(*creds, 21)["status"] == "PENDING"

    engine.cashfree_webhook("SUBSCRIPTION_AUTH_STATUS", auth_event(m["subscription_id"])["data"], 30)
    assert engine.mandate_status(*creds, 31)["status"] == "ACTIVE"

    sess, _, _ = trip(engine, creds, now=40)
    charge = fake.pays[-1]
    assert charge["payment_type"] == "CHARGE" and charge["subscription_id"] == m["subscription_id"]
    assert charge["payment_amount"] == sess.total / 100
    assert sess.state == "exited" and sess.receipt["method"] == "UPI Reserve Pay"


def test_wrong_secret_cannot_link_or_read_a_mandate(engine):
    _, creds, _ = trip(engine)
    with pytest.raises(EngineError) as e:
        engine.link_mandate(creds[0], "nope", {}, None, 20)
    assert e.value.status == 401
    with pytest.raises(EngineError):
        engine.mandate_status(creds[0], "nope", 20)


def test_declined_debit_and_over_limit_basket_go_to_the_desk(engine, fake):
    _, creds, _ = trip(engine)
    m, _ = engine.link_mandate(*creds, {}, None, 20)
    engine.cashfree_webhook("SUBSCRIPTION_AUTH_STATUS", auth_event(m["subscription_id"])["data"], 21)
    fake.decline = True
    sess, _, notes = trip(engine, creds, now=30)
    assert sess.state == "confirming" and any("insufficient" in n.data.get("text", "") for n in notes)

    fake.decline = False
    engine.payments.max_amount_paise = 1
    sess, _, notes = trip(engine, creds, now=40)
    assert sess.state == "confirming" and any("limit" in n.data.get("text", "") for n in notes)


def test_late_debit_failure_flags_the_trip_for_follow_up(engine, fake):
    _, creds, _ = trip(engine)
    m, _ = engine.link_mandate(*creds, {}, None, 20)
    engine.cashfree_webhook("SUBSCRIPTION_AUTH_STATUS", auth_event(m["subscription_id"])["data"], 21)
    sess, _, _ = trip(engine, creds, now=30)
    notes = engine.cashfree_webhook("SUBSCRIPTION_PAYMENT_FAILED", {"payment": {
        "payment_id": sess.receipt["payment_id"], "subscription_id": m["subscription_id"]}}, 40)
    assert sess.receipt["gateway_status"] == "FAILED"
    assert notes[0].data["level"] == "warn" and sess.session_id in notes[0].data["text"]


def test_webhook_signature_and_age_are_checked(cashfree):
    raw, headers = signed({"type": "x"})
    now = int(headers["x-webhook-timestamp"]) / 1000
    assert cashfree.webhook_ok(raw, headers["x-webhook-signature"], headers["x-webhook-timestamp"], now)
    assert not cashfree.webhook_ok(raw + b" ", headers["x-webhook-signature"], headers["x-webhook-timestamp"], now)
    assert not cashfree.webhook_ok(raw, headers["x-webhook-signature"], headers["x-webhook-timestamp"], now + 600)
    assert not cashfree.webhook_ok(raw, None, None, now)


def test_http_link_webhook_then_zero_touch_exit(tmp_path, cashfree, fake):
    settings = Settings()
    settings.data_dir, settings.staff_pin = tmp_path, "1111"
    app = create_app(settings, payments=cashfree)
    with TestClient(app) as c:
        assert c.get("/api/config").json()["payments"]["flow"] == "mandate"
        first = Phone(c)
        creds = {"shopper_id": first.shopper["shopper_id"], "shopper_secret": first.shopper["shopper_secret"]}

        r = c.post("/api/shoppers/mandate", json={**creds, "name": "Asha"})
        assert r.status_code == 200, r.text
        assert r.json()["approve_url"] == "https://sandbox.cashfree.com/approve/x"
        sub_id = r.json()["subscription_id"]
        assert c.post("/api/shoppers/mandate", json={**creds, "shopper_secret": "x"}).status_code == 401

        raw, headers = signed(auth_event(sub_id))
        assert c.post("/api/payments/cashfree/webhook", content=raw,
                      headers={**headers, "x-webhook-signature": "AAAA"}).status_code == 400
        assert c.post("/api/payments/cashfree/webhook", content=raw, headers=headers).status_code == 200
        assert c.post("/api/shoppers/mandate/status", json=creds).json()["status"] == "ACTIVE"

        p = Phone(c, shopper=creds)
        p.scan(MILK)
        p.exit()
        cart = p.cart()
        assert cart["state"] == "exited" and cart["receipt"]["provider"] == "cashfree_reserve_pay"


def test_reserve_pay_refunds_go_through_cashfree(engine, fake):
    _, creds, _ = trip(engine)
    m, _ = engine.link_mandate(*creds, {}, None, 20)
    engine.cashfree_webhook("SUBSCRIPTION_AUTH_STATUS", auth_event(m["subscription_id"])["data"], 21)
    sess, _, _ = trip(engine, creds, now=30)
    line = sess.live_lines[0]
    engine.refund(sess.session_id, [line.line_id], "damaged", 40)
    sent = fake.refunds[-1]
    assert sent["subscription_id"] == m["subscription_id"] and sent["payment_id"] == sess.receipt["payment_id"]
    assert sent["refund_amount"] == line.price / 100 and sent["cf_payment_id"] == 123
    assert sess.receipt["refunds"][-1]["status"] == "pending"

    fake.refund_status = "FAILED"
    sess2, _, _ = trip(engine, creds, now=50)
    with pytest.raises(EngineError) as e:
        engine.refund(sess2.session_id, [sess2.live_lines[0].line_id], "x", 60)
    assert e.value.status == 502
