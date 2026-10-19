"""The five demo scenes, plus the edge cases they depend on, driven with a fake clock."""
import pytest

from server.catalog import Catalog
from server.config import ROOT, Settings
from server.engine import Engine, EngineError
from server.ledger import Ledger
from server.payments import SandboxPayments
from server.security import TokenSigner, eid_for, sign_scan, verify_token

BARCODE = {"razor": "8901000000011", "blades": "8901000000028", "chocolate": "8901000000035",
           "gum": "8901000000059", "milk": "8901000000066"}


@pytest.fixture
def engine(tmp_path):
    settings = Settings()
    settings.match_window_s = 60
    return Engine(settings, Catalog.load(ROOT / "data" / "catalog.json"), Ledger(":memory:"),
                  TokenSigner(tmp_path / "key.pem"), SandboxPayments())


class Shopper:
    """Drives one phone session the way the web app does."""

    def __init__(self, engine, now=0.0, shopper_id=None, secret=None):
        self.e = engine
        self.sess, self.creds, _ = engine.start_session(shopper_id, secret, True, now)
        self.seq = 0

    @property
    def sid(self):
        return self.sess.session_id

    def go(self, aisle, now):
        self.e.set_location(self.sid, aisle, now)

    def scan(self, sku, now, package_label=None, confidence=None):
        self.seq += 1
        sig = sign_scan(self.sess.key, self.sid, self.seq, BARCODE[sku], 1000 + self.seq)
        line, notes = self.e.scan(self.sid, self.seq, BARCODE[sku], 1000 + self.seq, sig, now,
                                  package_label, confidence)
        return line, notes

    def exit(self, now, gate="G1"):
        token, _ = self.e.checkout(self.sid, now)
        payload = verify_token(token, self.e.signer.public_key, now)
        return self.e.gate_tap(gate, payload, now)


def take(engine, sku, now, shelf="S7-A"):
    return engine.shelf_event("st-1", shelf, "take", now, sku=sku, event_ts=now)


def kinds(notes, channel_prefix=""):
    return [n.kind for n in notes if n.channel.startswith(channel_prefix)]


def test_honest_shopper_walks_out_green_and_is_charged(engine):
    s = Shopper(engine)
    s.go("7", 1)
    take(engine, "razor", 2)
    line, _ = s.scan("razor", 5)
    assert line.status == "verified"
    notes = s.exit(10)
    assert any(n.kind == "gate" and n.data["light"] == "green" for n in notes)
    assert s.sess.state == "exited"
    assert s.sess.receipt["total"] == 29900
    assert s.sess.receipt["payment_id"].startswith("pay_test_")


def test_scan_before_shelf_event_still_matches(engine):
    s = Shopper(engine)
    s.go("7", 1)
    line, _ = s.scan("chocolate", 2)
    assert line.status == "scanned"
    take(engine, "chocolate", 3)
    assert line.status == "verified"


def test_forgot_to_scan_gets_a_nudge_then_resolves_green(engine):
    s = Shopper(engine)
    s.go("7", 0)
    take(engine, "chocolate", 1)
    assert engine.tick(30) == []
    notes = engine.tick(62)
    assert "nudge" in kinds(notes, f"phone:{s.sid}")
    s.scan("chocolate", 70)
    assessment = engine.assess(s.sid)
    assert assessment["decision"] == "green"
    assert assessment["points"] == engine.s.nudge_points


def test_barcode_swap_is_flagged_by_fusion(engine):
    s = Shopper(engine)
    s.go("7", 0)
    take(engine, "razor", 1)
    line, notes = s.scan("gum", 5)  # gum lives in aisle 2; shopper is in aisle 7 holding a razor
    assert line.status == "flagged"
    assert "nudge" in kinds(notes, f"phone:{s.sid}")
    assert engine.assess(s.sid)["decision"] == "red"


def test_swap_flag_softens_when_real_item_is_scanned(engine):
    s = Shopper(engine)
    s.go("7", 0)
    take(engine, "razor", 1)
    gum, _ = s.scan("gum", 5)
    s.scan("razor", 8)
    a = engine.assess(s.sid)
    assert a["points"] == engine.s.resolved_swap_points
    assert a["decision"] == "green"
    assert gum.status == "scanned"


def test_package_check_mismatch_is_flagged(engine):
    s = Shopper(engine)
    s.go("7", 0)
    line, _ = s.scan("gum", 2, package_label="blades", confidence=0.93)
    assert line.status == "flagged"
    assert engine.assess(s.sid)["points"] >= engine.s.package_mismatch_points


def test_pocketed_item_with_ignored_nudge_goes_red_and_opens_staff_check(engine):
    s = Shopper(engine)
    s.go("7", 0)
    take(engine, "razor", 1)
    engine.tick(62)
    notes = s.exit(90)
    assert any(n.kind == "gate" and n.data["light"] == "red" for n in notes)
    assert s.sid in engine.checks
    engine.staff_resolve(s.sid, "items_added", [engine.checks[s.sid]["unresolved"][0]["pick_id"]], 120)
    assert s.sess.state == "exited"
    assert s.sess.receipt["total"] == 29900


def test_unscanned_item_without_nudge_asks_shopper_to_confirm(engine):
    s = Shopper(engine)
    s.go("7", 0)
    take(engine, "chocolate", 1)
    notes = s.exit(10)  # before the match window ends
    assert "confirm" in kinds(notes, f"phone:{s.sid}")
    pick_id = next(n for n in notes if n.kind == "confirm").data["items"][0]["pick_id"]
    engine.confirm(s.sid, take=[pick_id], deny=[], now=12)
    assert s.sess.state == "exited"
    assert s.sess.receipt["total"] == 19000


def test_denying_a_sole_high_value_pick_escalates_to_staff(engine):
    s = Shopper(engine)
    s.go("7", 0)
    take(engine, "blades", 1)
    notes = s.exit(10)
    pick_id = next(n for n in notes if n.kind == "confirm").data["items"][0]["pick_id"]
    engine.confirm(s.sid, take=[], deny=[pick_id], now=12)
    assert s.sess.state == "staff_check"
    engine.staff_resolve(s.sid, "cleared", [], 20)
    assert engine.stats["red_cleared"] == 1


def test_put_back_cancels_the_expected_scan(engine):
    s = Shopper(engine)
    s.go("7", 0)
    take(engine, "razor", 1)
    engine.shelf_event("st-1", "S7-A", "return", 3, sku="razor")
    assert engine.tick(70) == []
    assert engine.assess(s.sid)["points"] == 0


def test_put_back_after_scanning_removes_the_item(engine):
    s = Shopper(engine)
    s.go("7", 0)
    take(engine, "razor", 1)
    line, _ = s.scan("razor", 3)
    engine.shelf_event("st-1", "S7-A", "return", 20, sku="razor")
    assert line.status == "removed"
    assert s.sess.total == 0


def test_two_shoppers_share_a_pick_until_one_scans(engine):
    a, b = Shopper(engine, shopper_id="a"), Shopper(engine, shopper_id="b")
    a.go("7", 0)
    b.go("7", 0)
    take(engine, "chocolate", 1)
    assert engine.assess(a.sid)["points"] == engine.assess(b.sid)["points"] > 0
    b.scan("chocolate", 4)
    assert engine.assess(a.sid)["points"] == 0
    assert engine.assess(b.sid)["points"] == 0


def test_tampered_or_replayed_scans_are_rejected(engine):
    s = Shopper(engine)
    with pytest.raises(EngineError) as bad_sig:
        engine.scan(s.sid, 1, BARCODE["blades"], 1, "00" * 32, 1)
    assert bad_sig.value.status == 401
    s.scan("milk", 2)
    sig = sign_scan(s.sess.key, s.sid, 1, BARCODE["milk"], 1001)
    with pytest.raises(EngineError) as replay:
        engine.scan(s.sid, 1, BARCODE["milk"], 1001, sig, 3)
    assert replay.value.status == 409


def test_exit_token_is_verifiable_offline_and_tamper_evident(engine):
    s = Shopper(engine)
    s.scan("milk", 1)
    token, _ = engine.checkout(s.sid, 2)
    payload = verify_token(token, engine.signer.public_key, 3)
    assert payload["total"] == 6800 and payload["decision"] == "green"
    body, sig = token.split(".")
    forged = body[:-2] + ("A" if body[-2] != "A" else "B") + body[-1] + "." + sig
    with pytest.raises(Exception):
        verify_token(forged, engine.signer.public_key, 3)


def test_failed_payment_falls_back_to_paying_in_app(engine):
    engine.payments.fail_next = 1
    s = Shopper(engine)
    s.scan("milk", 1)
    notes = s.exit(2)
    assert "payment_failed" in kinds(notes)
    engine.pay_in_app(s.sid, 5)
    assert s.sess.state == "exited"


def _twenty_five_point_trip(engine, creds, t0):
    s = Shopper(engine, now=t0, shopper_id=creds[0], secret=creds[1])
    s.go("7", t0)
    take(engine, "razor", t0 + 1)
    s.scan("gum", t0 + 2)       # swap flag, softened to 10 points once the razor is scanned
    s.scan("razor", t0 + 3)
    take(engine, "blades", t0 + 4)
    s.scan("milk", t0 + 5)      # second swap flag, also softened to 10
    s.scan("blades", t0 + 6)
    take(engine, "chocolate", t0 + 7)
    engine.tick(t0 + 70)        # nudge: 5 points
    s.scan("chocolate", t0 + 75)
    return engine.assess(s.sid)


def test_trusted_shopper_gets_a_higher_green_threshold(engine):
    first = _twenty_five_point_trip(engine, (None, None), 0)
    assert first["points"] == 25 and first["decision"] == "amber"
    loyal_creds = engine.ledger.register_shopper()
    engine.ledger.set_clean_streak(loyal_creds[0], engine.s.trusted_after_clean_trips)
    loyal = _twenty_five_point_trip(engine, loyal_creds, 1000)
    assert loyal["trusted"] is True and loyal["decision"] == "green"


def test_shared_pick_does_not_block_green_but_a_sole_one_does(engine):
    a, b = Shopper(engine, shopper_id="a"), Shopper(engine, shopper_id="b")
    a.go("7", 0)
    b.go("7", 0)
    take(engine, "chocolate", 1)
    assert engine.assess(a.sid)["decision"] == "green"  # half of 20 points, shared
    b.go("3", 2)
    take(engine, "chocolate", 3)  # only a is in aisle 7 now
    assert engine.assess(a.sid)["decision"] == "amber"


def test_ledger_chain_detects_edits(engine):
    s = Shopper(engine)
    s.scan("milk", 1)
    assert engine.ledger.verify_chain()
    engine.ledger._db.execute("UPDATE events SET payload='{}' WHERE id=2")
    assert not engine.ledger.verify_chain()


def test_walking_to_another_aisle_is_not_spoofing(engine):
    """A phone that just moved is still heard by the old aisle's shelf for a few seconds: no mismatch flag."""
    sess, _, _ = engine.start_session(None, None, True, 0)
    engine.set_location(sess.session_id, "7", 0)
    engine.presence("st", "S7-A", [eid_for(sess.key, 1)], 1)
    engine.set_location(sess.session_id, "3", 20)       # walks to aisle 3
    engine.presence("st", "S7-A", [eid_for(sess.key, 22)], 22)  # aisle-7 shelf still catches it briefly
    assert not any("reported aisle" in r for r in engine.assess(sess.session_id)["reasons"])


def test_shared_pick_not_me_moves_it_to_the_others(engine):
    a, _, _ = engine.start_session(None, None, True, 0)
    b, _, _ = engine.start_session(None, None, True, 0)
    for x in (a, b):
        engine.set_location(x.session_id, "7", 0)
    engine.shelf_event("st", "S7-A", "take", 1, lane=1)
    pick = next(iter(engine.picks.values()))
    assert sorted(pick.candidates) == sorted([a.session_id, b.session_id])
    engine.not_me(a.session_id, pick.pick_id, 70)
    assert pick.candidates == [b.session_id]
    engine.not_me(b.session_id, pick.pick_id, 71)        # the last one left can't pass it on
    assert pick.candidates == [b.session_id]
