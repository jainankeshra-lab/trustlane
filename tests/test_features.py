"""Features added after the gap analysis: persistence, staff tools, ID checks, loose produce,
floor scale, refunds, disputes, monitoring alerts, audit retention, weight learning and ERP sync."""
import asyncio
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from server.alerts import AlertNotifier
from server.catalog import Catalog
from server.config import ROOT, Settings
from server.engine import Engine
from server.ledger import Ledger
from server.main import create_app
from server.payments import SandboxPayments
from server.security import TokenSigner, sign_scan, verify_token
from tests.helpers import Phone, shelf, staff
from tests.test_api import make_settings

BC = {"razor": "8901000000011", "beer": "8901000000080", "tomatoes": "4664", "milk": "8901000000066"}


@pytest.fixture
def engine(tmp_path):
    return Engine(Settings(), Catalog.load(ROOT / "data" / "catalog.json"), Ledger(":memory:"),
                  TokenSigner(tmp_path / "k.pem"), SandboxPayments())


def trip(engine, aisle, now=0.0):
    sess, _, _ = engine.start_session(None, None, True, now)
    engine.set_location(sess.session_id, aisle, now)
    sess.test_seq = 0
    return sess


def scan(engine, sess, sku, now):
    sess.test_seq += 1
    sig = sign_scan(sess.key, sess.session_id, sess.test_seq, BC[sku], sess.test_seq)
    return engine.scan(sess.session_id, sess.test_seq, BC[sku], sess.test_seq, sig, now)[0]


def exit_trip(engine, sess, now, weight=None):
    token, _ = engine.checkout(sess.session_id, now)
    return engine.gate_tap("G1", verify_token(token, engine.signer.public_key, now), now, weight)


# ---------------------------------------------------------------- persistence
def test_trips_survive_a_server_restart(tmp_path):
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as c:
        p = Phone(c, aisle="7")
        shelf(c, lane=0)
        p.scan(BC["razor"])
    with TestClient(create_app(settings)) as c2:  # "restart": new process state, same data dir
        p.c = c2
        cart = p.cart()
        assert cart["items"] == 1 and cart["lines"][0]["status"] == "verified"
        p.exit()
        assert p.cart()["state"] == "exited"


# ---------------------------------------------------------------- age-restricted items
def test_age_restricted_item_needs_an_id_check_at_the_gate(engine):
    s = trip(engine, "5")
    engine.shelf_event("st", "S5-A", "take", 1, lane=1)
    scan(engine, s, "beer", 2)
    notes = exit_trip(engine, s, 5)
    assert s.state == "staff_check" and engine.checks[s.session_id]["kind"] == "id"
    assert any(n.kind == "gate" and "ID" in n.data["text"] for n in notes)
    engine.staff_resolve(s.session_id, "id_verified", [], 10)
    assert s.state == "exited" and s.receipt["total"] == 18000
    assert engine.stats["red_cleared"] == 0  # ID checks don't distort the fairness metric


def test_no_id_means_the_item_is_not_sold(engine):
    s = trip(engine, "5")
    scan(engine, s, "beer", 2)
    scan(engine, s, "milk", 3)
    exit_trip(engine, s, 5)
    engine.staff_resolve(s.session_id, "id_refused", [], 10)
    assert s.state == "exited" and s.receipt["total"] == 6800


def test_id_checked_in_the_aisle_means_a_green_exit(engine):
    s = trip(engine, "5")
    scan(engine, s, "beer", 2)
    engine.verify_id(s.session_id, 3)
    exit_trip(engine, s, 5)
    assert s.state == "exited"


# ---------------------------------------------------------------- loose produce
def test_loose_produce_is_priced_by_the_shelf_weight(engine):
    s = trip(engine, "3")
    engine.shelf_event("st", "S3-P", "take", 1, lane=0, weight_delta_g=-530)
    line = scan(engine, s, "tomatoes", 2)
    assert line.status == "verified" and line.grams == 530 and line.price == 2120  # Rs 40/kg
    engine.shelf_event("st", "S3-P", "return", 3, lane=0, weight_delta_g=200)  # puts some back
    assert line.grams == 330 and line.price == 1320


def test_produce_scanned_before_weighing_waits_for_the_shelf(engine):
    s = trip(engine, "3")
    line = scan(engine, s, "tomatoes", 1)
    assert line.status == "weighing"
    with pytest.raises(Exception):
        engine.checkout(s.session_id, 2)  # can't leave while weighing
    engine.shelf_event("st", "S3-P", "take", 3, lane=0, weight_delta_g=-1000)
    assert line.price == 4000


# ---------------------------------------------------------------- floor scale
def test_exit_floor_scale_flags_a_heavier_basket(engine):
    s = trip(engine, "3")
    scan(engine, s, "milk", 1)  # 1040 g expected
    exit_trip(engine, s, 5, weight=1100)
    assert s.state == "exited"
    s2 = trip(engine, "3", 10)
    scan(engine, s2, "milk", 11)
    exit_trip(engine, s2, 15, weight=2600)  # 1.5 kg of unscanned goods in the basket
    assert s2.state != "exited"
    assert any("Floor scale" in r for r in engine.assess(s2.session_id)["reasons"])


# ---------------------------------------------------------------- staff tools
def test_dead_phone_trip_is_closed_at_the_staff_desk(engine):
    s = trip(engine, "7")
    scan(engine, s, "razor", 1)
    engine.shelf_event("st", "S7-A", "take", 2, lane=2)  # chocolate taken, phone dies before scanning
    pick = engine.trip_detail(s.session_id, 3)["assessment"]["unresolved"][0]["pick_id"]
    engine.staff_close(s.session_id, [pick], 5)
    assert s.state == "exited" and s.receipt["provider"] == "desk" and s.receipt["total"] == 29900 + 19000


def test_empty_trip_can_be_cancelled_but_not_one_with_items(engine):
    s = trip(engine, "3")
    engine.cancel_trip(s.session_id, 1)
    assert s.state == "cancelled"
    s2 = trip(engine, "3")
    scan(engine, s2, "milk", 1)
    with pytest.raises(Exception):
        engine.cancel_trip(s2.session_id, 2)


def test_refund_returns_money_and_stock(engine):
    s = trip(engine, "3")
    line = scan(engine, s, "milk", 1)
    exit_trip(engine, s, 2)
    stock_after_sale = engine.stock["milk"]
    engine.refund(s.session_id, [line.line_id], "damaged", 5)
    assert line.status == "refunded" and s.receipt["refunds"][0]["amount"] == 6800
    assert engine.stock["milk"] == stock_after_sale + 1


def test_dispute_is_reviewed_and_can_refund(engine):
    s = trip(engine, "3")
    line = scan(engine, s, "milk", 1)
    exit_trip(engine, s, 2)
    notes = engine.open_dispute(s.session_id, "I was charged for milk I put back", 3)
    did = next(n.data for n in notes if n.kind == "dispute")["dispute_id"]
    engine.resolve_dispute(did, "upheld", "Refunded the milk", [line.line_id], 4)
    assert engine.disputes[did]["status"] == "upheld" and line.status == "refunded"


# ---------------------------------------------------------------- monitoring
def test_new_alert_rules(engine):
    engine.heartbeat("st-1", "S7-A", 0, {"lanes": [{"lane": 0, "drift_g": 7.5}], "camera_fps": 3.0})
    s = trip(engine, "7")
    engine.shelf_event("st-1", "S7-A", "take", 1, lane=1)  # Rs 699 blades, never scanned
    engine.stats.update({"payments_ok": 18, "payments_failed": 3, "green": 18, "red": 3})
    alerts = {a["id"].split(":")[0] for a in engine.summary(100, True, buffered=600)["alerts"]}
    assert {"drift", "fps", "high_value", "payments", "red_exits", "buffer"} <= alerts


def test_alerts_page_once_and_report_recovery():
    sent = []
    transport = httpx.MockTransport(lambda req: sent.append(req.content) or httpx.Response(200))
    n = AlertNotifier(slack_webhook="https://hooks.example/x", client=httpx.AsyncClient(transport=transport))
    a = [{"id": "latency", "level": "bad", "text": "slow"}, {"id": "silent", "level": "info", "text": "fyi"}]

    async def flow():
        await n.update(a, 1)
        await n.update(a, 2)       # still firing: no repeat page
        await n.update([], 3)      # recovered
    asyncio.run(flow())
    assert len(sent) == 2 and b"slow" in sent[0] and b"Resolved" in sent[1]


def test_audit_retention_keeps_the_chain_verifiable():
    ledger = Ledger(":memory:")
    for t in range(10):
        ledger.append(t * 100.0, "x", None, {"n": t})
    assert ledger.prune(450) == 5 and ledger.count() == 5
    assert ledger.verify_chain()
    ledger.append(2000, "y", None, {})
    assert ledger.verify_chain()


def test_catalog_learns_real_packaging_weight(engine):
    trip(engine, "7")
    for i in range(15):
        engine.shelf_event("st", "S7-A", "take", i, lane=0, weight_delta_g=-58)  # razors weigh 58 g, not 52
    assert abs(engine.learned["razor"]["mean"] - 58) < 1
    assert any(a["id"] == "weight:razor" for a in engine.summary(20, True)["alerts"])


# ---------------------------------------------------------------- ERP sync
def test_erp_stock_prices_and_sales_export(tmp_path):
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as c:
        before = {r["sku"]: r["stock"] for r in c.get("/api/erp/stock", headers=staff(app)).json()}["milk"]
        p = Phone(c, aisle="3")
        p.scan(BC["milk"])
        p.exit()
        after = {r["sku"]: r["stock"] for r in c.get("/api/erp/stock", headers=staff(app)).json()}["milk"]
        assert after == before - 1
        r = c.post("/api/erp/prices", content="sku,price_rupees\nmilk,70\n", headers=staff(app))
        assert r.json()["changed"] == ["milk"]
        assert next(x for x in c.get("/api/catalog").json() if x["sku"] == "milk")["price"] == 7000
        csv = c.get("/api/erp/sales.csv", headers=staff(app)).text
        assert f"{p.sid},milk,6800" in csv


def test_phone_offline_queue_replays_after_reaching_the_exit(tmp_path):
    with TestClient(create_app(make_settings(tmp_path))) as c:
        p = Phone(c, aisle="3")
        p.post("/checkout")                       # phone reached the exit with a scan still queued
        assert p.scan(BC["milk"]).status_code == 409
        assert p.scan(BC["milk"], offline=True).status_code == 200  # flushed from the phone's signed log


def test_finished_trips_leave_live_memory_but_stay_reachable(engine):
    sess = trip(engine, "7", 0)
    scan(engine, sess, "razor", 1)
    exit_trip(engine, sess, 2)
    assert sess.state == "exited"
    assert engine.compact(100)["archived_trips"] == 0       # just finished: stays for an hour
    assert engine.compact(4000)["archived_trips"] == 1
    assert sess.session_id not in engine.sessions and not engine.picks
    detail = engine.trip_detail(sess.session_id, 4001)         # staff open it: loaded back from the archive
    assert detail["shopper_id"] == sess.shopper_id and engine.sessions[sess.session_id].receipt["total"] == 29900
    assert sess.session_id in engine.sessions
    assert any(t["session_id"] == sess.session_id for t in engine.trips_for(sess.shopper_id))
