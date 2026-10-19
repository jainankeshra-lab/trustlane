"""Fusion and risk engine.

Matches shelf picks (camera + weight) to phone scans, keeps each shopper's virtual
cart, scores the trip, and decides at the exit gate: green, amber or red.

The engine is plain synchronous Python. Every method takes `now` so tests can drive
time, and returns a list of Notes that the web layer pushes over WebSockets.
`snapshot()` / `restore()` let the server save live trips and survive a restart.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
from pathlib import Path
from collections import deque
from dataclasses import asdict, dataclass, field

from . import metrics
from .catalog import Catalog, Product
from .config import Settings
from .ledger import Ledger
from .security import (TokenSigner, cart_hash, eid_matches, new_session_id, new_session_key,
                       verify_scan_signature)

WEIGHT_RESIDUE_G = 20  # a loose-produce pick with less than this left counts as fully put back
log = logging.getLogger("trustlane")
EVENT_LOG = logging.getLogger("trustlane.events")  # JSON lines, shipped to Loki by Promtail


@dataclass
class Note:
    channel: str  # "phone:<session_id>", "gate:<gate_id>" or "ops"
    kind: str
    data: dict


@dataclass
class CartLine:
    line_id: str
    sku: str
    name: str
    price: int
    status: str  # verified | scanned | weighing | flagged | removed | refunded
    pick_id: str | None = None
    note: str = ""
    grams: float | None = None  # loose produce: weight from the shelf
    age_restricted: bool = False
    return_ts: float = 0.0      # the shopper tapped "Put back" in the app; the shelf confirms it

    def public(self) -> dict:
        return asdict(self)


@dataclass
class Pick:
    pick_id: str
    sku: str
    shelf_id: str
    aisle: str
    ts: float
    candidates: list[str]
    status: str = "open"  # open | matched | returned | released
    matched_session: str | None = None
    nudged: bool = False
    denied: list[str] = field(default_factory=list)  # shoppers who answered the nudge "Not me"
    swap_flagged: bool = False
    evidence: str = ""     # how the product was identified: weight only, camera confirmed, ...
    attribution: str = ""  # how the shoppers were chosen: presence | aisle | silent | scan
    grams: float | None = None


@dataclass
class RiskEvent:
    ts: float
    kind: str
    points: float
    reason: str
    pick_id: str | None = None
    line_id: str | None = None
    resolved_points: float | None = None  # set when the shopper later fixes the issue

    @property
    def current_points(self) -> float:
        return self.points if self.resolved_points is None else self.resolved_points


@dataclass
class PendingScan:
    line_id: str
    sku: str
    ts: float


@dataclass
class Session:
    session_id: str
    shopper_id: str
    key: str
    created: float
    aisle: str | None = None          # self-reported by the phone
    aisle_ts: float = 0.0
    aisle_since: float = 0.0          # when the phone moved to its current aisle
    presence_aisle: str | None = None  # observed: a shelf station detected the phone's Bluetooth ID
    presence_shelf: str | None = None
    presence_ts: float = 0.0
    last_seq: int = 0
    lines: list[CartLine] = field(default_factory=list)
    risk: list[RiskEvent] = field(default_factory=list)
    pending_scans: list[PendingScan] = field(default_factory=list)
    state: str = "shopping"  # shopping | at_exit | confirming | staff_check | paying | exited | cancelled
    gate_id: str | None = None
    receipt: dict | None = None
    payment_order: dict | None = None  # gateway order awaiting the shopper's approval
    pending: dict | None = None        # how the exit was decided, kept while payment is in flight
    pending_decision: str | None = None  # exit decision waiting on an ID check
    consent_ts: float = 0.0
    age_verified: bool = False
    mismatch_ts: float = 0.0
    push_token: str | None = None  # Expo push token from the native app
    coupon: str | None = None      # coupon code the shopper applied
    points_redeem: int = 0         # loyalty points the shopper chose to use on this trip
    discount: int = 0              # offers + coupon + points, set by Engine._reprice

    @property
    def subtotal(self) -> int:
        return sum(ln.price for ln in self.lines if ln.status not in ("removed", "refunded"))

    @property
    def total(self) -> int:
        """What the shopper pays: shelf prices (GST included) minus offers, coupon and points."""
        return max(0, self.subtotal - self.discount)

    @property
    def live_lines(self) -> list[CartLine]:
        return [ln for ln in self.lines if ln.status not in ("removed", "refunded")]

    @property
    def needs_id(self) -> bool:
        return any(ln.age_restricted for ln in self.live_lines)

    def line(self, line_id: str) -> CartLine | None:
        return next((ln for ln in self.lines if ln.line_id == line_id), None)


class EngineError(Exception):
    def __init__(self, status: int, message: str, notes: list[Note] | None = None):
        super().__init__(message)
        self.status = status
        self.notes = notes or []


def _session_from(d: dict) -> "Session":
    d = dict(d)
    d["lines"] = [_rebuild(CartLine, x) for x in d.get("lines", [])]
    d["risk"] = [_rebuild(RiskEvent, x) for x in d.get("risk", [])]
    d["pending_scans"] = [_rebuild(PendingScan, x) for x in d.get("pending_scans", [])]
    return _rebuild(Session, d)


def _rebuild(cls, data: dict):
    """Dataclass from saved data; fields added since it was saved take their defaults, unknown ones are ignored."""
    from dataclasses import fields
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in names})


class Engine:
    def __init__(self, settings: Settings, catalog: Catalog, ledger: Ledger, signer: TokenSigner, payments):
        self.s = settings
        self.catalog = catalog
        self.ledger = ledger
        self.signer = signer
        self.payments = payments
        self.sessions: dict[str, Session] = {}
        self.picks: dict[str, Pick] = {}
        self.stations: dict[str, dict] = {}
        self.checks: dict[str, dict] = {}    # open staff checks by session id
        self.disputes: dict[str, dict] = {}
        self.feed: deque[dict] = deque(maxlen=200)
        self.latencies: deque[float] = deque(maxlen=500)
        self.recent: deque[tuple[float, str]] = deque(maxlen=5000)  # (ts, "pick" | "nudge") for spike alerts
        self.stats = {"picks": 0, "unattributed": 0, "matched": 0, "nudges": 0, "scans": 0, "returns": 0,
                      "green": 0, "amber": 0, "red": 0, "red_resolved": 0, "red_cleared": 0,
                      "payments_ok": 0, "payments_failed": 0, "silent_attributions": 0, "refunds": 0}
        self.stock: dict[str, float] = {p.sku: 50.0 for p in catalog.all()}  # units, or kg for loose produce
        self.learned: dict[str, dict] = {}  # sku -> {"mean": grams per unit, "n": samples}
        self.presence_seen_ts = 0.0
        self.gates: dict[str, dict] = {}   # gate agents' heartbeats
        self.edges: dict[str, dict] = {}   # store edge processes' heartbeats
        from .invoice import Invoicer, load_store
        from .pricing import Offers
        self.offers_path = Path(settings.offers_path)
        self.offers = Offers.load(self.offers_path)
        self.store = load_store(settings.data_dir)
        self.invoicer = Invoicer(ledger, settings.data_dir, self.store)
        self.invoice_listeners: list = []  # called with (session, invoice) after each paid trip (e-mail receipts)
        self._counter = 0

    # ------------------------------------------------------------------ persistence
    def snapshot(self) -> dict:
        return {
            "counter": self._counter, "presence_seen_ts": self.presence_seen_ts,
            "sessions": [asdict(s) for s in self.sessions.values()],
            "picks": [asdict(p) for p in self.picks.values()],
            "stations": self.stations, "checks": self.checks, "disputes": self.disputes,
            "stats": self.stats, "stock": self.stock, "learned": self.learned,
            "feed": list(self.feed)[:100], "recent": list(self.recent)[-1000:],
        }

    def restore(self, data: dict) -> None:
        self._counter = data.get("counter", 0)
        self.presence_seen_ts = data.get("presence_seen_ts", 0.0)
        self.sessions = {}
        for d in data.get("sessions", []):
            sess = _session_from(d)
            self.sessions[sess.session_id] = sess
        self.picks = {d["pick_id"]: _rebuild(Pick, d) for d in data.get("picks", [])}
        self.stations = data.get("stations", {})
        self.checks = data.get("checks", {})
        self.disputes = data.get("disputes", {})
        self.stats.update(data.get("stats", {}))
        self.stock.update(data.get("stock", {}))
        self.learned = data.get("learned", {})
        self.feed = deque(data.get("feed", []), maxlen=200)
        self.recent = deque((tuple(x) for x in data.get("recent", [])), maxlen=5000)

    # ------------------------------------------------------------------ helpers
    def _id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}{self._counter:05d}"

    def _get(self, session_id: str) -> Session:
        sess = self.sessions.get(session_id)
        if not sess:
            data = self.ledger.load_trip(session_id)  # a finished trip moved out of memory (refund, dispute)
            if not data:
                raise EngineError(404, "No such shopping session")
            sess = self.sessions[session_id] = _session_from(data)
        return sess

    def compact(self, now: float, keep_finished_s: float = 3600) -> dict:
        """Keep live state small: finished trips go to the archive table after an hour, and resolved
        shelf picks are dropped once nobody can put the item back against them any more. Without this
        every state snapshot (and so every request) gets slower as the day goes on."""
        archived = 0
        for sid, sess in list(self.sessions.items()):
            if sess.state not in ("exited", "cancelled"):
                continue
            ended = (sess.receipt or {}).get("ts") or sess.created
            if now - ended < keep_finished_s or any(d["session_id"] == sid and d["status"] == "open"
                                                    for d in self.disputes.values()):
                continue
            self.ledger.archive_trip(sid, sess.shopper_id, ended, asdict(sess))
            del self.sessions[sid]
            archived += 1
        horizon = self.s.return_lookback_s + 300
        stale = [pid for pid, p in self.picks.items() if p.status != "open" and now - p.ts > horizon]
        for pid in stale:
            del self.picks[pid]
        return {"archived_trips": archived, "dropped_picks": len(stale)}

    def _log(self, now: float, kind: str, text: str, session_id: str | None = None,
             level: str = "info", **payload) -> Note:
        self.ledger.append(now, kind, session_id, {"text": text, **payload})
        entry = {"ts": now, "kind": kind, "text": text, "session_id": session_id, "level": level}
        self.feed.appendleft(entry)
        EVENT_LOG.info(json.dumps(entry))
        return Note("ops", "feed", entry)

    def _cart_note(self, sess: Session) -> Note:
        return Note(f"phone:{sess.session_id}", "cart", self.cart_view(sess.session_id))

    def _add_risk(self, sess: Session, now: float, kind: str, points: float, reason: str,
                  pick_id: str | None = None, line_id: str | None = None) -> list[Note]:
        sess.risk.append(RiskEvent(now, kind, points, reason, pick_id, line_id))
        metrics.FLAGS.labels(kind).inc()
        return [self._log(now, f"risk.{kind}", reason, sess.session_id, level="warn", points=points)]

    def _open_picks_for(self, session_id: str) -> list[Pick]:
        return sorted((p for p in self.picks.values() if p.status == "open" and session_id in p.candidates),
                      key=lambda p: p.ts)

    def _pick_value(self, pick: Pick) -> int:
        return self.catalog.get(pick.sku).price_for(pick.grams)

    def _unit_weight(self, product: Product) -> float:
        """Packaging weight learned from real picks once there are enough samples, else the catalog's."""
        learned = self.learned.get(product.sku)
        return learned["mean"] if learned and learned["n"] >= 10 else product.unit_weight_g

    def _update_gauges(self, now: float) -> None:
        metrics.ACTIVE_SESSIONS.set(sum(1 for s in self.sessions.values() if s.state not in ("exited", "cancelled")))
        metrics.OPEN_DISCREPANCIES.set(sum(
            1 for p in self.picks.values()
            if p.status == "open" and p.candidates and now - p.ts >= self.s.match_window_s))

    # ------------------------------------------------------------------ sessions & location
    def start_session(self, shopper_id: str | None, shopper_secret: str | None, consent: bool,
                      now: float) -> tuple[Session, tuple[str, str] | None, list[Note]]:
        """Returns the trip, plus new shopper credentials when the phone had none (or wrong ones)."""
        if not consent:
            raise EngineError(400, "Please accept the privacy notice to start a trip")
        creds = None
        if not self.ledger.check_shopper(shopper_id, shopper_secret):
            creds = self.ledger.register_shopper()
            shopper_id = creds[0]
        sess = Session(new_session_id(), shopper_id, new_session_key(), now, consent_ts=now)
        self.sessions[sess.session_id] = sess
        streak = self.ledger.clean_streak(shopper_id)
        note = self._log(now, "session.start", f"Shopper entered ({streak} clean trips so far)", sess.session_id)
        self._update_gauges(now)
        return sess, creds, [note]

    def set_location(self, session_id: str, aisle: str, now: float) -> list[Note]:
        sess = self._get(session_id)
        changed = sess.aisle != aisle
        if sess.aisle != aisle:
            sess.aisle_since = now
        sess.aisle, sess.aisle_ts = aisle, now
        notes = [self._log(now, "session.location", f"Phone reports aisle {aisle}", session_id)] if changed else []
        return notes + self._check_mismatch(sess, now)

    def register_push(self, session_id: str, token: str, now: float) -> list[Note]:
        sess = self._get(session_id)
        if not token.startswith(("ExponentPushToken[", "ExpoPushToken[")) or len(token) > 200:
            raise EngineError(400, "Not an Expo push token")
        sess.push_token = token
        return [self._log(now, "push.registered", "Phone registered for push notifications", session_id)]

    def _presence_fresh(self, sess: Session, now: float) -> bool:
        """A recent Bluetooth sighting that the phone hasn't since contradicted by moving to another aisle."""
        if not sess.presence_ts or now - sess.presence_ts > self.s.presence_fresh_s:
            return False
        moved_after = sess.aisle and sess.aisle != sess.presence_aisle and sess.aisle_since > sess.presence_ts
        return not moved_after

    def presence_active(self, now: float) -> bool:
        """True while shelf stations are reporting Bluetooth detections."""
        return now - self.presence_seen_ts <= 2 * self.s.presence_fresh_s

    def presence(self, station_id: str, shelf_id: str, eids: list[str], now: float) -> list[Note]:
        """A shelf station heard these rotating Bluetooth IDs nearby. Only the server can map them to trips."""
        self.presence_seen_ts = now
        aisle = next((p.aisle for p in self.catalog.all() if p.shelf_id == shelf_id), None)
        notes: list[Note] = []
        for sess in self.sessions.values():
            if sess.state == "shopping" and any(eid_matches(sess.key, e, now) for e in eids):
                notes += self._mark_presence(sess, shelf_id, aisle, now, "shelf heard the phone")
        return notes

    def heard_shelf(self, session_id: str, shelf_id: str, now: float) -> list[Note]:
        """The phone heard this shelf's current signed token (the server already checked it)."""
        sess = self._get(session_id)
        if sess.state != "shopping":
            return []
        self.presence_seen_ts = now
        aisle = next((p.aisle for p in self.catalog.all() if p.shelf_id == shelf_id), None)
        return self._mark_presence(sess, shelf_id, aisle, now, "phone heard the shelf")

    def _mark_presence(self, sess: Session, shelf_id: str, aisle: str | None, now: float, how: str) -> list[Note]:
        notes: list[Note] = []
        if sess.presence_aisle != aisle:
            notes.append(self._log(now, "session.presence", f"Detected near shelf {shelf_id} (aisle {aisle}, {how})",
                                   sess.session_id))
        sess.presence_aisle, sess.presence_shelf, sess.presence_ts = aisle, shelf_id, now
        return notes + self._check_mismatch(sess, now)

    def _check_mismatch(self, sess: Session, now: float) -> list[Note]:
        """The phone claims one aisle while a shelf detected it in another: likely spoofed location."""
        if not (self._presence_fresh(sess, now) and sess.aisle and now - sess.aisle_ts <= self.s.location_fresh_s):
            return []
        if sess.presence_ts < sess.aisle_since + 10:  # still walking between aisles
            return []
        if sess.aisle == sess.presence_aisle or now - sess.mismatch_ts < 120:
            return []
        sess.mismatch_ts = now
        return self._add_risk(sess, now, "location_mismatch", self.s.location_mismatch_points,
                              f"Phone reported aisle {sess.aisle}, but shelf {sess.presence_shelf} "
                              f"detected it in aisle {sess.presence_aisle}")

    # ------------------------------------------------------------------ pricing: offers, coupon, points, GST
    def _reprice(self, sess: Session) -> dict:
        from .pricing import price_basket
        items = [{"line_id": ln.line_id, "sku": ln.sku, "price": ln.price} for ln in sess.live_lines]
        products = {p.sku: p for p in self.catalog.all()}
        pricing = price_basket(items, products, self.offers, sess.coupon, sess.points_redeem)
        sess.discount = pricing["discount_total"]
        return pricing

    def apply_coupon(self, session_id: str, code: str, now: float) -> list[Note]:
        sess = self._get(session_id)
        if sess.state not in ("shopping", "at_exit"):
            raise EngineError(409, "Coupons can be added while shopping")
        code = (code or "").strip().upper()
        if not code:
            sess.coupon = None
            return [self._cart_note(sess)]
        rule = self.offers.coupons.get(code)
        if not rule:
            raise EngineError(404, "That coupon code isn't valid")
        if rule.get("once_per_shopper") and self.ledger.coupon_used(sess.shopper_id, code):
            raise EngineError(409, "You've already used this coupon")
        sess.coupon = code
        return [self._log(now, "coupon.applied", f"Coupon {code} applied", session_id), self._cart_note(sess)]

    def set_points(self, session_id: str, points: int, now: float) -> list[Note]:
        sess = self._get(session_id)
        if sess.state not in ("shopping", "at_exit"):
            raise EngineError(409, "Points can be used while shopping")
        balance, rule = self.ledger.points(sess.shopper_id), self.offers.loyalty
        if points < 0 or points > balance:
            raise EngineError(400, f"You have {balance} points")
        if 0 < points < rule["min_redeem"]:
            raise EngineError(400, f"Use at least {rule['min_redeem']} points at a time")
        sess.points_redeem = points
        return [self._cart_note(sess)]

    def cart_view(self, session_id: str) -> dict:
        sess = self._get(session_id)
        pricing = self._reprice(sess)
        return {
            "session_id": sess.session_id,
            "state": sess.state,
            "aisle": sess.aisle,
            "lines": [ln.public() for ln in sess.lines],
            "subtotal": pricing["subtotal"],
            "discounts": pricing["discounts"],
            "coupon": pricing["coupon"],
            "points": {"balance": self.ledger.points(sess.shopper_id), "redeemed": pricing["points_redeemed"],
                       "will_earn": pricing["points_earned"], **self.offers.loyalty},
            "gst": pricing["gst"],
            "offers": [r.get("label", r["id"]) for r in self.offers.promotions],
            "total": sess.total,
            "items": len(sess.live_lines),
            "needs_id": sess.needs_id and not sess.age_verified,
            "receipt": sess.receipt,
        }

    # ------------------------------------------------------------------ phone scans
    def scan(self, session_id: str, seq: int, barcode: str, client_ts: int, sig: str, now: float,
             package_label: str | None = None, package_confidence: float | None = None,
             replayed: bool = False) -> tuple[CartLine, list[Note]]:
        """`replayed` marks a scan buffered during an outage (by the gateway or the phone): it still
        counts even if the shopper reached the exit before it arrived."""
        sess = self._get(session_id)
        if sess.state != "shopping" and not (replayed and sess.state not in ("exited", "cancelled")):
            raise EngineError(409, "This trip is already at the exit")
        if not verify_scan_signature(sess.key, session_id, seq, barcode, client_ts, sig):
            metrics.SCANS_REJECTED.labels("signature").inc()
            notes = self._add_risk(sess, now, "tamper", self.s.tamper_points,
                                   "Scan rejected: signature did not match the session key")
            raise EngineError(401, "Scan signature is not valid", notes)
        if seq <= sess.last_seq:
            metrics.SCANS_REJECTED.labels("replay").inc()
            raise EngineError(409, "Scan was already received (replayed sequence number)")
        product = self.catalog.by_barcode(barcode)
        if not product:
            metrics.SCANS_REJECTED.labels("unknown_barcode").inc()
            raise EngineError(404, "Unknown barcode")
        sess.last_seq = seq

        line = CartLine(self._id("L"), product.sku, product.name, 0 if product.by_weight else product.price,
                        "weighing" if product.by_weight else "scanned", age_restricted=product.age_restricted)
        if product.by_weight:
            line.note = "Weight comes from the produce shelf"
        sess.lines.append(line)
        self.stats["scans"] += 1
        metrics.SCANS.labels(str(product.monitored).lower()).inc()
        notes = [self._log(now, "scan", f"Scanned {product.name}", session_id, sku=product.sku, seq=seq)]

        held = self.catalog.get(package_label) if package_label else None  # "empty" / unknown labels say nothing
        if held and held.sku != product.sku and (package_confidence or 0) >= self.s.package_min_confidence:
            held_name = held.name
            line.status, line.note = "flagged", f"Package looks like {held_name}"
            notes += self._add_risk(sess, now, "package_mismatch", self.s.package_mismatch_points,
                                    f"Package check: barcode says {product.name}, package looks like {held_name}",
                                    line_id=line.line_id)
            notes.append(Note(f"phone:{session_id}", "nudge", {
                "text": f"That barcode is for {product.name}, but the package looks like {held_name}. "
                        f"Please scan the barcode printed on the item.",
                "sku": package_label}))

        if product.age_restricted and not sess.age_verified:
            notes.append(Note(f"phone:{session_id}", "info", {
                "text": f"{product.name} is age-restricted. Please show your ID to a team member before you leave."}))
            notes.append(self._log(now, "id.needed", f"ID check needed ({product.name})", session_id, level="warn"))

        # Several open picks of this product may name this shopper (busy aisle). Match the one most
        # likely to be theirs: the fewest other possible takers, then the most recent.
        options = [p for p in self._open_picks_for(session_id) if p.sku == product.sku]
        pick = min(options, key=lambda p: (len(p.candidates), -p.ts), default=None)
        if pick:
            notes += self._match(pick, sess, line, now)
        else:
            if product.monitored:
                sess.pending_scans.append(PendingScan(line.line_id, product.sku, now))
            notes += self._check_swap(sess, product, line, now)

        notes.append(self._cart_note(sess))
        self._update_gauges(now)
        return line, notes

    def _match(self, pick: Pick, sess: Session, line: CartLine, now: float) -> list[Note]:
        others = [c for c in pick.candidates if c != sess.session_id]
        pick.status, pick.matched_session = "matched", sess.session_id
        line.pick_id = pick.pick_id
        product = self.catalog.get(pick.sku)
        if product.by_weight:
            line.grams = pick.grams or 0
            line.price = product.price_for(line.grams)
            line.name, line.note = f"{product.name} · {line.grams:.0f} g", ""
        if line.status in ("scanned", "weighing"):
            line.status = "verified"
        self.stats["matched"] += 1
        metrics.MATCHES.inc()
        # The shopper scanned the real item after all: soften any swap flag tied to this pick or line.
        for ev in sess.risk:
            if ev.kind == "swap_suspect" and ev.resolved_points is None and (
                    ev.pick_id == pick.pick_id or ev.line_id == line.line_id):
                ev.resolved_points = self.s.resolved_swap_points
                flagged = sess.line(ev.line_id) if ev.line_id else None
                if flagged and flagged.status == "flagged":
                    flagged.status = "verified" if flagged is line else "scanned"
                    flagged.note = ""
        notes = [self._log(now, "match", f"{line.name}: shelf pick matched to scan", sess.session_id,
                           pick_id=pick.pick_id)]
        notes.append(Note(f"phone:{sess.session_id}", "resolved", {"pick_id": pick.pick_id, "sku": pick.sku}))
        for other in others:
            notes.append(Note(f"phone:{other}", "resolved", {"pick_id": pick.pick_id, "sku": pick.sku}))
        return notes

    def _check_swap(self, sess: Session, scanned: Product, line: CartLine, now: float) -> list[Note]:
        """A scan with no matching pick, while the shopper holds a pricier unscanned item
        picked in another place, looks like a barcode swap."""
        if scanned.by_weight:
            return []
        for pick in self._open_picks_for(sess.session_id):
            held = self.catalog.get(pick.sku)
            if pick.swap_flagged or held is None or held.by_weight or now - pick.ts > self.s.match_window_s:
                continue
            if len(pick.candidates) != 1:  # a shared pick may be someone else's: no swap accusation
                continue
            if held.price <= scanned.price:
                continue
            if not (scanned.monitored or scanned.aisle != pick.aisle):
                continue
            pick.swap_flagged = True
            line.status, line.note = "flagged", f"Shelf saw {held.name} taken"
            notes = self._add_risk(sess, now, "swap_suspect", self.s.swap_points,
                                   f"Scanned {scanned.name} right after taking {held.name} from shelf {pick.shelf_id}",
                                   pick_id=pick.pick_id, line_id=line.line_id)
            notes.append(Note(f"phone:{sess.session_id}", "nudge", {
                "text": f"You scanned {scanned.name}, but the shelf saw you take {held.name}. "
                        f"Please scan the {held.name}.",
                "sku": held.sku, "pick_id": pick.pick_id}))
            return notes
        return []

    # ------------------------------------------------------------------ shopper changes their mind
    def put_back(self, session_id: str, line_id: str, now: float) -> list[Note]:
        """The shopper taps "Put back" on a cart line. Items on monitored shelves come off the cart when the
        shelf feels them return (so a tap alone can't make a pocketed item free); others come off at once."""
        sess = self._get(session_id)
        if sess.state != "shopping":
            raise EngineError(409, "You can change your cart while shopping")
        line = sess.line(line_id)
        if not line or line.status in ("removed", "refunded"):
            raise EngineError(404, "That item isn't in your cart")
        product = self.catalog.get(line.sku)
        if product and product.monitored and not product.by_weight:
            line.return_ts = now
            return [Note(f"phone:{session_id}", "info", {"text": f"Put the {line.name} back on its shelf: "
                                                                 f"it comes off your cart when the shelf feels it."}),
                    self._cart_note(sess)]
        return self._remove_line(sess, line, now, "Removed by the shopper")

    def _remove_line(self, sess: Session, line: CartLine, now: float, note: str) -> list[Note]:
        line.status, line.note, line.return_ts = "removed", note, 0.0
        if line.pick_id and line.pick_id in self.picks:
            self.picks[line.pick_id].status = "returned"
        sess.pending_scans = [ps for ps in sess.pending_scans if ps.line_id != line.line_id]
        return [self._log(now, "return", f"{line.name} put back; cart updated", sess.session_id, line_id=line.line_id),
                Note(f"phone:{sess.session_id}", "info", {"text": f"{line.name} is off your cart."}),
                self._cart_note(sess)]

    # ------------------------------------------------------------------ gate agents and store edges
    def gate_heartbeat(self, gate_id: str, info: dict, now: float) -> None:
        self.gates[gate_id] = {**self.gates.get(gate_id, {}), **info, "gate_id": gate_id, "last_seen": now}
        metrics.GATE_LAST_SEEN.labels(gate_id).set(now)
        metrics.GATE_OFFLINE_QUEUE.labels(gate_id).set(info.get("offline_queue") or 0)

    def edge_heartbeat(self, edge_id: str, info: dict, now: float) -> None:
        self.edges[edge_id] = {**info, "edge_id": edge_id, "last_seen": now}
        metrics.EDGE_LAST_SEEN.labels(edge_id).set(now)
        metrics.EDGE_OUTBOX.labels(edge_id).set(info.get("outbox") or 0)

    # ------------------------------------------------------------------ shelf events
    def heartbeat(self, station_id: str, shelf_id: str, now: float, info: dict | None = None) -> None:
        entry = self.stations.get(station_id, {})
        entry.update({"station_id": station_id, "shelf_id": shelf_id, "last_seen": now})
        if info:
            entry.update({k: v for k, v in info.items() if v is not None})
        self.stations[station_id] = entry
        metrics.STATION_LAST_SEEN.labels(station_id).set(now)

    def shelf_event(self, station_id: str, shelf_id: str, kind: str, now: float, sku: str | None = None,
                    lane: int | None = None, qty: int = 1, event_ts: float | None = None,
                    hand_seen: bool | None = None, weight_delta_g: float | None = None,
                    vision_sku: str | None = None, vision_confidence: float | None = None) -> list[Note]:
        product = self.catalog.get(sku) if sku else self.catalog.by_lane(shelf_id, lane if lane is not None else -1)
        if not product:
            raise EngineError(404, "Unknown product for this shelf event")
        if kind not in ("take", "return"):
            raise EngineError(400, "kind must be take or return")
        self.heartbeat(station_id, shelf_id, now)
        if event_ts is not None:
            latency = max(0.0, now - event_ts)
            self.latencies.append(latency)
            metrics.EVENT_LATENCY.observe(latency)
        notes: list[Note] = []
        if product.by_weight:  # loose produce: one event, any weight
            grams = abs(weight_delta_g) if weight_delta_g is not None else product.unit_weight_g
            notes += (self._take(product, shelf_id, now, hand_seen, "weighed", grams) if kind == "take"
                      else self._return(product, shelf_id, now, grams))
            self._update_gauges(now)
            return notes
        evidence = ""
        if kind == "take":
            product, qty, evidence, vision_notes = self._fuse_vision(
                product, shelf_id, qty, now, weight_delta_g, vision_sku, vision_confidence)
            notes += vision_notes
            self._learn_weight(product, qty, weight_delta_g)
        for _ in range(max(1, qty)):
            notes += (self._take(product, shelf_id, now, hand_seen, evidence) if kind == "take"
                      else self._return(product, shelf_id, now))
        self._update_gauges(now)
        return notes

    def _learn_weight(self, product: Product, qty: int, weight_delta_g: float | None) -> None:
        """Self-learning catalog: track the real packaging weight from picks that clearly fit."""
        if weight_delta_g is None or qty < 1:
            return
        per_unit = abs(weight_delta_g) / qty
        if abs(per_unit - self._unit_weight(product)) > 0.25 * self._unit_weight(product):
            return
        entry = self.learned.setdefault(product.sku, {"mean": product.unit_weight_g, "n": 0})
        entry["n"] += 1
        entry["mean"] += (per_unit - entry["mean"]) / min(entry["n"], 20)  # running mean, then a moving one

    def _fuse_vision(self, lane_product: Product, shelf_id: str, qty: int, now: float,
                     weight_delta_g: float | None, vision_sku: str | None, vision_confidence: float | None
                     ) -> tuple[Product, int, str, list[Note]]:
        """Combine the load-cell lane (which product *should* be there) with what the camera saw.

        The camera wins only when it is confident AND the weight change fits the product it saw
        better than the lane's product. That catches items put back in the wrong lane without
        trusting a single noisy frame. On a tie (e.g. 160 g = 1 chocolate = 4 blade packs) the lane wins."""
        if not vision_sku:
            self._vision_result("none")
            return lane_product, qty, "weight only", []
        seen = self.catalog.get(vision_sku)
        conf = vision_confidence or 0.0
        if seen is None or seen.by_weight or conf < self.s.vision_min_confidence:
            self._vision_result("low_confidence")
            return lane_product, qty, f"weight only, camera unsure ({vision_sku} {conf:.2f})", []
        if seen.sku == lane_product.sku:
            self._vision_result("agree")
            return lane_product, qty, f"camera confirmed {conf:.2f}", []
        grams = abs(weight_delta_g) if weight_delta_g is not None else None

        def fit(unit_g: float) -> tuple[int, float]:
            """Whole units that best explain the weight change, and the error as a share of one unit."""
            n = max(1, round(grams / unit_g))
            return n, abs(grams - n * unit_g) / unit_g

        if grams is None:
            fits, seen_qty = True, qty
        else:
            seen_qty, seen_err = fit(self._unit_weight(seen))
            _, lane_err = fit(self._unit_weight(lane_product))
            fits = seen_err <= self.s.vision_weight_tolerance and seen_err < lane_err
        if fits:
            self._vision_result("override")
            note = self._log(now, "vision.misplaced",
                             f"Camera saw {seen.name} taken from the {lane_product.name} lane on {shelf_id} "
                             f"(weight fits): misplaced item, restock that lane", level="warn")
            return seen, max(1, seen_qty), f"camera saw {seen.name} {conf:.2f}, overrides lane", [note]
        self._vision_result("disagree")
        note = self._log(now, "vision.disagree",
                         f"Camera saw {seen.name} but the weight fits {lane_product.name} as well or better "
                         f"on {shelf_id}; kept the lane product", level="warn")
        return lane_product, qty, f"camera disagreed ({seen.name} {conf:.2f})", [note]

    def _vision_result(self, result: str) -> None:
        self.stats[f"vision_{result}"] = self.stats.get(f"vision_{result}", 0) + 1
        metrics.VISION_CHECKS.labels(result).inc()

    def _candidates(self, product: Product, now: float) -> tuple[list[str], str]:
        """Who could have taken this item. Every kind of evidence adds suspects; none hides anyone:
        1. phones a shelf station detected in this aisle (observed, hard to fake),
        2. phones that reported this aisle (unless Bluetooth places them elsewhere),
        3. phones whose location we can't vouch for at all. Going silent, or switching Bluetooth off
           while stations are listening, makes you a suspect, not invisible, even if someone else is
           standing at the shelf."""
        shopping = [s for s in self.sessions.values() if s.state == "shopping"]
        presence = [s for s in shopping if self._presence_fresh(s, now) and s.presence_aisle == product.aisle]
        aisle = [s for s in shopping if s not in presence and s.aisle == product.aisle
                 and now - s.aisle_ts <= self.s.attribution_window_s
                 and not (self._presence_fresh(s, now) and s.presence_aisle != product.aisle)]
        located = presence + aisle
        if self.presence_active(now):
            silent = [s for s in shopping if not self._presence_fresh(s, now)
                      and (s.aisle is None or now - s.aisle_ts > self.s.location_fresh_s)]
        else:
            silent = [s for s in shopping if s.aisle is None or now - s.aisle_ts > self.s.location_fresh_s]
        silent = [] if located else silent  # suspects only when nobody can be placed at the shelf
        how = "presence" if presence and not aisle else "aisle" if located else "silent"
        if located and silent:
            how += "+silent"
        ids = [s.session_id for s in located + silent]
        # A shopper who already scanned this product counts too, whatever their location says.
        for s in shopping:
            if s.session_id not in ids and any(
                    ps.sku == product.sku and now - ps.ts <= self.s.match_window_s for ps in s.pending_scans):
                ids.append(s.session_id)
                how = how if ids[:-1] else "scan"
        return ids, how

    def _is_silent(self, sess: Session, now: float) -> bool:
        if self._presence_fresh(sess, now):
            return False
        return sess.aisle is None or now - sess.aisle_ts > self.s.location_fresh_s

    def _take(self, product: Product, shelf_id: str, now: float, hand_seen: bool | None,
              evidence: str = "", grams: float | None = None) -> list[Note]:
        cands, how = self._candidates(product, now)
        pick = Pick(self._id("P"), product.sku, shelf_id, product.aisle, now, cands, evidence=evidence,
                    attribution=how, grams=grams)
        self.picks[pick.pick_id] = pick
        self.stats["picks"] += 1
        self.recent.append((now, "pick"))
        metrics.PICKS.labels(str(bool(cands)).lower()).inc()
        what = f"{product.name}" + (f" {grams:.0f} g" if grams is not None else "")
        parts = [p for p in (evidence, None if hand_seen is None else ("hand seen" if hand_seen else "no hand seen")) if p]
        hand = f" ({'; '.join(parts)})" if parts else ""
        if not cands:
            self.stats["unattributed"] += 1
            return [self._log(now, "pick.unattributed", f"{what} taken from {shelf_id}, no shopper in the store",
                              level="warn", pick_id=pick.pick_id)]
        who = cands[0] if len(cands) == 1 else f"{len(cands)} shoppers"
        notes = [self._log(now, "pick", f"{what} taken from {shelf_id} by {who} [{how}]{hand}",
                           cands[0] if len(cands) == 1 else None, pick_id=pick.pick_id)]
        silent_ids = [sid for sid in cands if self._is_silent(self.sessions[sid], now)]
        if silent_ids:
            self.stats["silent_attributions"] += 1
            for sid in silent_ids:
                notes += self._add_risk(self.sessions[sid], now, "silent_location",
                                        self.s.silent_location_points / len(cands),
                                        f"Location unknown while {product.name} was taken in aisle {product.aisle}",
                                        pick_id=pick.pick_id)
        # The shopper may have scanned before the shelf reported the pick.
        for sid in cands:
            sess = self.sessions[sid]
            pending = next((ps for ps in sess.pending_scans
                            if ps.sku == product.sku and now - ps.ts <= self.s.match_window_s), None)
            if pending:
                sess.pending_scans.remove(pending)
                line = sess.line(pending.line_id)
                if line and line.status != "removed":
                    notes += self._match(pick, sess, line, now)
                    notes.append(self._cart_note(sess))
                    break
        return notes

    def _located_at(self, aisle: str, now: float) -> set[str]:
        """Shoppers we can place in this aisle right now (Bluetooth presence, or a fresh aisle report)."""
        return {x.session_id for x in self.sessions.values() if x.state == "shopping" and (
            (self._presence_fresh(x, now) and x.presence_aisle == aisle)
            or (x.aisle == aisle and now - x.aisle_ts <= self.s.location_fresh_s))}

    def _return(self, product: Product, shelf_id: str, now: float, grams: float | None = None) -> list[Note]:
        """The shelf saw one unit come back. It can't see who, so: first a shopper who tapped "Put back"
        in the app, then picks of shoppers standing in this aisle, then the oldest open pick."""
        self.stats["returns"] += 1
        metrics.RETURNS.inc()
        here = self._located_at(product.aisle, now)
        if not product.by_weight:
            intent = [(ln, x) for x in self.sessions.values() if x.state == "shopping" for ln in x.live_lines
                      if ln.sku == product.sku and ln.return_ts and now - ln.return_ts <= 180]
            if intent:
                ln, x = min(intent, key=lambda lx: lx[0].return_ts)
                return self._remove_line(x, ln, now, "Put back on the shelf")
        open_picks = sorted((p for p in self.picks.values()
                             if p.status == "open" and p.sku == product.sku and p.shelf_id == shelf_id),
                            key=lambda p: (not (set(p.candidates) & here), p.ts))
        matched_here = [p for p in self.picks.values()
                        if p.status == "matched" and p.sku == product.sku and p.shelf_id == shelf_id
                        and p.matched_session in here and now - p.ts <= self.s.return_lookback_s]
        # A pick still unscanned after half a minute is usually not the one coming back (honest shoppers
        # scan within seconds): if someone standing here has already scanned this product, it's theirs.
        fresh_open = open_picks and now - open_picks[0].ts <= 30 and set(open_picks[0].candidates) & here
        if matched_here and not fresh_open:
            open_picks = []
        if open_picks:
            pick = open_picks[0]
            if product.by_weight and grams is not None and (pick.grams or 0) - grams > WEIGHT_RESIDUE_G:
                pick.grams -= grams  # put some of the produce back
                return [self._log(now, "return", f"{grams:.0f} g of {product.name} put back; {pick.grams:.0f} g still taken",
                                  pick_id=pick.pick_id)]
            pick.status = "returned"
            notes = [self._log(now, "return", f"{product.name} put back on {shelf_id}; no scan needed",
                               pick.candidates[0] if len(pick.candidates) == 1 else None, pick_id=pick.pick_id)]
            notes += [Note(f"phone:{sid}", "resolved", {"pick_id": pick.pick_id, "sku": pick.sku})
                      for sid in pick.candidates]
            return notes
        # Otherwise the shopper is putting back something they already scanned: take it off their cart.
        matched = [p for p in self.picks.values()
                   if p.status == "matched" and p.sku == product.sku and p.shelf_id == shelf_id
                   and now - p.ts <= self.s.return_lookback_s
                   and self.sessions[p.matched_session].state == "shopping"]
        matched.sort(key=lambda p: (p.matched_session in here, self.sessions[p.matched_session].aisle == product.aisle,
                                    p.ts), reverse=True)
        if matched:
            pick = matched[0]
            sess = self.sessions[pick.matched_session]
            line = next((ln for ln in sess.lines if ln.pick_id == pick.pick_id and ln.status != "removed"), None)
            if product.by_weight and line and grams is not None and (line.grams or 0) - grams > WEIGHT_RESIDUE_G:
                line.grams -= grams
                pick.grams = line.grams
                line.price = product.price_for(line.grams)
                line.name = f"{product.name} · {line.grams:.0f} g"
                text = f"We saw you put back some {product.name}; your cart now has {line.grams:.0f} g."
            else:
                pick.status = "returned"
                if line:
                    line.status, line.note = "removed", "Put back on the shelf"
                text = f"We saw you put back the {product.name}, so it's off your cart."
            return [
                self._log(now, "return", f"{product.name} put back; cart updated", sess.session_id, pick_id=pick.pick_id),
                Note(f"phone:{sess.session_id}", "info", {"text": text}),
                self._cart_note(sess),
            ]
        return [self._log(now, "return.unmatched", f"{product.name} placed on {shelf_id} (restock or misplaced item)")]

    # ------------------------------------------------------------------ timers
    def tick(self, now: float) -> list[Note]:
        notes: list[Note] = []
        for pick in list(self.picks.values()):
            if pick.status != "open" or pick.nudged or not pick.candidates:
                continue
            if now - pick.ts < self.s.match_window_s:
                continue
            pick.nudged = True
            product = self.catalog.get(pick.sku)
            self.stats["nudges"] += 1
            self.recent.append((now, "nudge"))
            metrics.NUDGES.inc()
            text = (f"The shelf recorded a {product.name} taken while we couldn't see where you were. "
                    f"If you picked it up, please scan it.") if pick.attribution == "silent" else \
                f"Did you pick up the {product.name}? Please scan it, or put it back."
            for sid in pick.candidates:
                notes.append(Note(f"phone:{sid}", "nudge", {"text": text, "sku": pick.sku, "pick_id": pick.pick_id}))
            if len(pick.candidates) == 1:
                notes += self._add_risk(self.sessions[pick.candidates[0]], now, "nudge", self.s.nudge_points,
                                        f"Nudged to scan {product.name}", pick_id=pick.pick_id)
            else:
                notes.append(self._log(now, "nudge", f"Nudged {len(pick.candidates)} shoppers about {product.name}"))
        self._update_gauges(now)
        return notes

    def not_me(self, session_id: str, pick_id: str, now: float) -> list[Note]:
        """The shopper answers a nudge "I didn't take it". On a shared pick they step aside, but only while
        someone who hasn't said so remains, so nobody can push a pick onto a neighbour just by tapping first.
        On a pick only they could have taken, it stays theirs and goes to the exit check."""
        sess = self._get(session_id)
        pick = self.picks.get(pick_id)
        if not pick or pick.status != "open" or session_id not in pick.candidates:
            raise EngineError(404, "Nothing to answer for that item")
        if session_id not in pick.denied:
            pick.denied.append(session_id)
        others = [c for c in pick.candidates if c not in pick.denied]
        product = self.catalog.get(pick.sku)
        notes = [self._log(now, "nudge.not_me", f"Shopper says they didn't take the {product.name}", session_id,
                           pick_id=pick_id)]
        if others:
            pick.candidates.remove(session_id)
            notes.append(Note(f"phone:{session_id}", "resolved", {"pick_id": pick_id, "sku": pick.sku}))
        else:
            notes.append(Note(f"phone:{session_id}", "info", {
                "text": f"Thanks. The shelf can't tell who took the {product.name}, so it'll be checked at the exit."}))
        return notes + [self._cart_note(sess)]

    # ------------------------------------------------------------------ risk
    def assess(self, session_id: str) -> dict:
        sess = self._get(session_id)
        points, reasons, unresolved = 0.0, [], []
        for ev in sess.risk:
            if ev.current_points:
                points += ev.current_points
                reasons.append(ev.reason + (" (fixed later)" if ev.resolved_points is not None else ""))
        for pick in self._open_picks_for(session_id):
            product = self.catalog.get(pick.sku)
            share = 1 / len(pick.candidates)
            pts = (self.s.theft_points[product.theft] + (self.s.ignored_nudge_points if pick.nudged else 0)) * share
            points += pts
            what = product.name + (f" ({pick.grams:.0f} g)" if pick.grams is not None else "")
            reasons.append(f"{what} taken from shelf {pick.shelf_id} but not scanned"
                           + (", nudge ignored" if pick.nudged else "")
                           + (f" (shared with {len(pick.candidates) - 1} other shopper)" if share < 1 else ""))
            unresolved.append({"pick_id": pick.pick_id, "sku": pick.sku, "name": what,
                               "price": self._pick_value(pick), "shared": share < 1})
        trusted = self.ledger.clean_streak(sess.shopper_id) >= self.s.trusted_after_clean_trips
        green_max = self.s.green_max_trusted if trusted else self.s.green_max
        decision = "green" if points < green_max else "amber" if points < self.s.amber_max else "red"
        # Never auto-charge past an item only this shopper could have taken: ask them to confirm it.
        if decision == "green" and any(not u["shared"] for u in unresolved):
            decision = "amber"
        return {"points": round(points, 1), "decision": decision, "reasons": reasons,
                "unresolved": unresolved, "trusted": trusted}

    # ------------------------------------------------------------------ exit
    def checkout(self, session_id: str, now: float) -> tuple[str, list[Note]]:
        sess = self._get(session_id)
        if sess.state not in ("shopping", "at_exit"):
            raise EngineError(409, "This trip is already being checked out")
        if any(ln.status == "weighing" for ln in sess.lines):
            raise EngineError(409, "Loose produce is still being weighed: wait a moment, or put it on the produce shelf again")
        sess.state = "at_exit"
        self._reprice(sess)
        a = self.assess(session_id)
        payload = {
            "sid": sess.session_id,
            "total": sess.total,
            "items": len(sess.live_lines),
            "cart": cart_hash([(ln.line_id, ln.sku, ln.price) for ln in sess.live_lines]),
            "decision": "red" if sess.needs_id and not sess.age_verified and a["decision"] == "green" else a["decision"],
            "iat": int(now),
            "exp": int(now) + self.s.token_ttl_s,
        }
        token = self.signer.sign(payload)
        return token, [self._log(now, "exit.token", "Exit token issued", session_id, decision=a["decision"])]

    def resume_shopping(self, session_id: str, now: float) -> list[Note]:
        sess = self._get(session_id)
        if sess.state == "at_exit":
            sess.state = "shopping"
        return [self._cart_note(sess)]

    def gate_tap(self, gate_id: str, payload: dict, now: float, basket_weight_g: float | None = None) -> list[Note]:
        """Called with a token payload the gate has already verified."""
        sess = self._get(payload["sid"])
        if sess.state in ("exited", "cancelled"):
            return [Note(f"gate:{gate_id}", "gate", {"light": "amber", "text": "This trip is already closed"})]
        sess.gate_id = gate_id
        notes: list[Note] = []
        if basket_weight_g is not None:
            notes += self._floor_scale(sess, basket_weight_g, now)
        a = self.assess(sess.session_id)
        decision = a["decision"]
        self.stats[decision] += 1
        metrics.EXIT_DECISIONS.labels(decision).inc()
        notes.append(self._log(now, f"exit.{decision}", f"Exit decision {decision.upper()} at {gate_id} "
                               f"({a['points']} risk points)", sess.session_id, reasons=a["reasons"]))
        return notes + self._proceed(sess, decision, a, now, how="auto", clean=a["points"] < self.s.nudge_points * 2)

    def _floor_scale(self, sess: Session, measured_g: float, now: float) -> list[Note]:
        """Optional floor scale in the exit lane: a basket much heavier than its scanned items is suspicious."""
        expected = sum(ln.grams if ln.grams is not None else self._unit_weight(self.catalog.get(ln.sku))
                       for ln in sess.live_lines)
        extra = measured_g - expected
        if extra <= max(150.0, 0.15 * expected):
            return [self._log(now, "exit.scale", f"Floor scale {measured_g:.0f} g fits the scanned items ({expected:.0f} g)",
                              sess.session_id)]
        return self._add_risk(sess, now, "basket_heavier", self.s.basket_heavier_points,
                              f"Floor scale: basket weighs {extra:.0f} g more than the scanned items")

    def _proceed(self, sess: Session, decision: str, a: dict, now: float, how: str, clean: bool) -> list[Note]:
        if decision != "red" and sess.needs_id and not sess.age_verified:
            sess.pending_decision = decision
            return self._open_check(sess, {**a, "reasons": ["Age-restricted items in the basket: ID not checked yet"]},
                                    now, sess.gate_id, kind="id")
        if decision == "green":
            return self._complete(sess, now, how=how, clean=clean)
        if decision == "amber":
            sess.state = "confirming"
            return [Note(f"phone:{sess.session_id}", "confirm", {"items": a["unresolved"], "reasons": a["reasons"]}),
                    Note(f"gate:{sess.gate_id}", "gate", {"light": "amber", "text": "Please confirm your items on your phone"})]
        return self._open_check(sess, a, now, sess.gate_id, kind="risk")

    def _open_check(self, sess: Session, a: dict, now: float, gate_id: str | None, kind: str = "risk") -> list[Note]:
        sess.state = "staff_check"
        self.checks[sess.session_id] = {"session_id": sess.session_id, "opened": now, "gate_id": gate_id, "kind": kind,
                                        "points": a["points"], "reasons": a["reasons"], "unresolved": a["unresolved"],
                                        "cart": self.cart_view(sess.session_id)}
        id_check = kind == "id"
        return [
            Note("ops", "check", self.checks[sess.session_id]),
            Note(f"phone:{sess.session_id}", "staff", {
                "text": "Please show your ID to a team member." if id_check else "A team member will be with you in a moment."}),
            Note(f"gate:{gate_id}", "gate", {"light": "amber" if id_check else "red",
                                             "text": "Please show your ID to a team member" if id_check
                                             else "A team member will help you shortly"}),
        ]

    def confirm(self, session_id: str, take: list[str], deny: list[str], now: float) -> list[Note]:
        sess = self._get(session_id)
        if sess.state != "confirming":
            raise EngineError(409, "Nothing to confirm")
        notes: list[Note] = []
        for pid in take:
            notes += self._add_from_pick(sess, pid, now, "Confirmed at exit")
        escalate = []
        for pid in deny:
            pick = self.picks.get(pid)
            if not pick or pick.status != "open" or session_id not in pick.candidates:
                continue
            product = self.catalog.get(pick.sku)
            if len(pick.candidates) == 1 and product.theft != "low":
                escalate.append(product.name)
            else:
                pick.candidates.remove(session_id)
                if not pick.candidates:
                    pick.status = "released"
        a = self.assess(session_id)
        if escalate:
            a["reasons"].append("Shopper says they did not take: " + ", ".join(escalate))
            self.stats["red"] += 1
            metrics.EXIT_DECISIONS.labels("red").inc()
            return notes + [self._log(now, "exit.red", "Shopper disputed shelf picks; staff check", session_id)] + \
                self._open_check(sess, a, now, sess.gate_id)
        return notes + self._proceed(sess, "green", a, now, how="confirmed", clean=False)

    def _add_from_pick(self, sess: Session, pick_id: str, now: float, note: str) -> list[Note]:
        pick = self.picks.get(pick_id)
        if not pick or pick.status != "open":
            return []
        product = self.catalog.get(pick.sku)
        line = CartLine(self._id("L"), product.sku, product.name, product.price_for(pick.grams), "verified",
                        note=note, age_restricted=product.age_restricted)
        sess.lines.append(line)
        return self._match(pick, sess, line, now)

    def _release_open_picks(self, session_id: str) -> None:
        for pick in self._open_picks_for(session_id):
            pick.candidates.remove(session_id)
            if not pick.candidates:
                pick.status = "released"

    def staff_resolve(self, session_id: str, outcome: str, take: list[str], now: float) -> list[Note]:
        sess = self._get(session_id)
        check = self.checks.get(session_id)
        if sess.state != "staff_check" or not check:
            raise EngineError(409, "No open staff check for this trip")
        if check.get("kind") == "id":
            if outcome not in ("id_verified", "id_refused"):
                raise EngineError(400, "outcome must be id_verified or id_refused for an ID check")
            self.checks.pop(session_id)
            notes = [Note("ops", "check_closed", {"session_id": session_id})]
            if outcome == "id_refused":
                for ln in sess.live_lines:
                    if ln.age_restricted:
                        ln.status, ln.note = "removed", "Not sold: no valid ID"
                notes.append(self._log(now, "id.refused", "No valid ID: age-restricted items removed", session_id))
            else:
                notes.append(self._log(now, "id.verified", "ID checked at the exit", session_id))
            sess.age_verified = True
            return notes + self._proceed(sess, sess.pending_decision or "green", self.assess(session_id), now,
                                         how="id_checked", clean=False)
        if outcome not in ("cleared", "items_added"):
            raise EngineError(400, "outcome must be cleared or items_added")
        self.stats["red_resolved"] += 1
        metrics.RED_RESOLVED.labels(outcome).inc()
        if outcome == "cleared":
            self.stats["red_cleared"] += 1
        notes = []
        for pid in take:
            notes += self._add_from_pick(sess, pid, now, "Added by staff at exit")
        self._release_open_picks(session_id)
        self.checks.pop(session_id, None)
        notes.append(self._log(now, f"check.{outcome}", f"Staff check closed: {outcome.replace('_', ' ')}", session_id))
        notes.append(Note("ops", "check_closed", {"session_id": session_id}))
        return notes + self._proceed(sess, "green", self.assess(session_id), now, how=f"staff_{outcome}", clean=False)

    # ------------------------------------------------------------------ staff tools
    def verify_id(self, session_id: str, now: float) -> list[Note]:
        """Staff checked the shopper's ID in the aisle, before they reach the gate."""
        sess = self._get(session_id)
        sess.age_verified = True
        notes = [self._log(now, "id.verified", "ID checked by staff", session_id),
                 Note(f"phone:{session_id}", "info", {"text": "Thanks, your ID has been checked."}),
                 self._cart_note(sess)]
        if self.checks.get(session_id, {}).get("kind") == "id":
            return notes + self.staff_resolve(session_id, "id_verified", [], now)
        return notes

    def trip_detail(self, session_id: str, now: float) -> dict:
        sess = self._get(session_id)
        return {
            **self.cart_view(session_id),
            "shopper_id": sess.shopper_id,
            "created": sess.created,
            "assessment": self.assess(session_id),
            "location": {"reported": sess.aisle, "reported_age_s": round(now - sess.aisle_ts, 1) if sess.aisle else None,
                         "detected": sess.presence_aisle,
                         "detected_age_s": round(now - sess.presence_ts, 1) if sess.presence_ts else None},
            "picks": [asdict(p) for p in self.picks.values() if session_id in p.candidates or p.matched_session == session_id],
            "disputes": [d for d in self.disputes.values() if d["session_id"] == session_id],
            "check": self.checks.get(session_id),
        }

    def staff_close(self, session_id: str, take: list[str], now: float) -> list[Note]:
        """Close a trip at the staff desk, e.g. the shopper's phone died: add confirmed items, take payment
        at the desk, release everything else."""
        sess = self._get(session_id)
        if sess.state in ("exited", "cancelled"):
            raise EngineError(409, "This trip is already closed")
        notes: list[Note] = []
        for pid in take:
            notes += self._add_from_pick(sess, pid, now, "Added at the staff desk")
        self._release_open_picks(session_id)
        self.checks.pop(session_id, None)
        sess.age_verified = True  # staff at the desk see the shopper
        sess.pending = {"how": "staff_desk", "clean": False}
        self._reprice(sess)
        notes.append(self._log(now, "trip.desk", "Trip closed at the staff desk", session_id))
        return notes + self._finalize(sess, now, {"payment_id": f"desk_{secrets.token_hex(5)}", "status": "captured",
                                                  "amount": sess.total, "method": "paid at staff desk",
                                                  "mode": "in store", "provider": "desk"})

    def cancel_trip(self, session_id: str, now: float) -> list[Note]:
        """Shopper left without buying anything (cart empty, nothing unresolved)."""
        sess = self._get(session_id)
        if sess.state in ("exited", "cancelled"):
            raise EngineError(409, "This trip is already closed")
        if sess.live_lines or any(len(p.candidates) == 1 for p in self._open_picks_for(session_id)):
            raise EngineError(409, "This trip has items or unresolved picks: close it at the desk instead")
        self._release_open_picks(session_id)
        self.checks.pop(session_id, None)
        sess.state = "cancelled"
        self._update_gauges(now)
        return [self._log(now, "trip.cancelled", "Trip cancelled by staff (nothing bought)", session_id),
                self._cart_note(sess)]

    def refund(self, session_id: str, line_ids: list[str], reason: str, now: float) -> list[Note]:
        sess = self._get(session_id)
        if sess.state != "exited" or not sess.receipt:
            raise EngineError(409, "Only a paid trip can be refunded")
        lines = [ln for ln in sess.live_lines if ln.line_id in line_ids]
        paid = sess.receipt.get("lines_net", {})
        amount = sum(paid.get(ln.line_id, ln.price) for ln in lines)  # what the shopper paid, after offers
        if not lines or amount <= 0:
            raise EngineError(400, "Choose at least one paid item to refund")
        provider = sess.receipt.get("provider")
        if provider in ("desk", None) or not sess.receipt.get("payment_id"):
            result = {"refund_id": f"desk_rfnd_{secrets.token_hex(5)}", "amount": amount, "status": "processed",
                      "provider": "desk"}
        else:
            from .payments import PaymentError
            try:
                result = self.payments.refund(sess.receipt["payment_id"], amount, receipt=sess.receipt)
            except PaymentError as err:
                raise EngineError(502, f"Refund failed: {err}") from err
        for ln in lines:
            ln.status, ln.note = "refunded", reason or "Refunded"
            self._restock(ln, +1)
        credit = None
        if sess.receipt.get("invoice"):
            credit = self.invoicer.credit_note(sess, sess.receipt["invoice"]["number"], [ln.line_id for ln in lines],
                                               reason or "Refund", now)
        sess.receipt.setdefault("refunds", []).append({**result, "lines": line_ids, "reason": reason, "ts": now,
                                                       "credit_note": credit})
        self.stats["refunds"] += 1
        return [self._log(now, "refund", f"Refunded Rs {amount / 100:.2f} ({reason or 'no reason given'})", session_id),
                Note(f"phone:{session_id}", "info", {"text": f"Refund of Rs {amount / 100:.2f} is on its way."}),
                self._cart_note(sess)]

    # ------------------------------------------------------------------ disputes (privacy: a way to contest a flag)
    def open_dispute(self, session_id: str, text: str, now: float) -> list[Note]:
        sess = self._get(session_id)
        text = (text or "").strip()[:500]
        if not text:
            raise EngineError(400, "Please say what you'd like to dispute")
        did = self._id("D")
        self.disputes[did] = {"dispute_id": did, "session_id": session_id, "text": text, "ts": now,
                              "status": "open", "state_at_dispute": sess.state}
        return [self._log(now, "dispute.open", f"Shopper dispute: {text}", session_id, level="warn"),
                Note("ops", "dispute", self.disputes[did]),
                Note(f"phone:{session_id}", "info", {"text": "Thanks. A team member will review this and get back to you."})]

    def resolve_dispute(self, dispute_id: str, outcome: str, note: str, refund_lines: list[str], now: float) -> list[Note]:
        d = self.disputes.get(dispute_id)
        if not d or d["status"] != "open":
            raise EngineError(404, "No open dispute with that id")
        if outcome not in ("upheld", "rejected"):
            raise EngineError(400, "outcome must be upheld or rejected")
        notes = self.refund(d["session_id"], refund_lines, f"Dispute {dispute_id}", now) if refund_lines else []
        d.update({"status": outcome, "resolution": note, "resolved_ts": now})
        return notes + [
            self._log(now, f"dispute.{outcome}", f"Dispute {dispute_id} {outcome}: {note}", d["session_id"]),
            Note(f"phone:{d['session_id']}", "info", {"text": f"Your dispute was reviewed: {outcome}. {note}".strip()}),
        ]

    # ------------------------------------------------------------------ shopper data (accounts, DPDP)
    def trips_for(self, shopper_id: str) -> list[dict]:
        live = list(self.sessions.values())
        archived = [_session_from(d) for d in self.ledger.archived_trips_for(shopper_id)
                    if d["session_id"] not in self.sessions]
        return [{"session_id": x.session_id, "state": x.state, "started": x.created, "total": x.total,
                 "items": [ln.name for ln in x.live_lines], "invoice": (x.receipt or {}).get("invoice")}
                for x in live + archived if x.shopper_id == shopper_id]

    def erase_shopper(self, shopper_id: str, now: float) -> tuple[dict, list[Note]]:
        """Right to erasure. Refused during an unfinished trip (it still has to be paid for)."""
        mine = [x for x in self.sessions.values() if x.shopper_id == shopper_id]
        if any(x.state not in ("exited", "cancelled") for x in mine):
            raise EngineError(409, "Finish or cancel your current trip first")
        mandate = self.ledger.mandate_for(shopper_id, now)
        if mandate and hasattr(self.payments, "cancel_mandate"):
            from .payments import PaymentError
            try:
                self.payments.cancel_mandate(mandate["subscription_id"])
            except PaymentError as err:
                raise EngineError(502, f"Couldn't cancel UPI Reserve Pay first: {err}") from err
        counts = self.ledger.erase_shopper(shopper_id)
        pseudonym = "erased-" + hashlib.sha256(shopper_id.encode()).hexdigest()[:12]
        for x in mine:
            x.shopper_id, x.push_token, x.aisle = pseudonym, None, None
        return counts, [self._log(now, "dpdp.erasure", f"Shopper data erased on request ({len(mine)} trips unlinked)",
                                  None, **counts)]

    # ------------------------------------------------------------------ staff audit
    def staff_action(self, username: str, role: str, path: str, now: float) -> list[Note]:
        """Audit-log one staff action (sign-in or change) by the person who did it."""
        m = re.search(r"/(?:trips|checks)/(T[0-9A-F]+)", path)
        action = path.removeprefix("/api/")
        return [self._log(now, "staff.action", f"{username} ({role}): {action}", m.group(1) if m else None,
                          staff=username, role=role, action=action)]

    # ------------------------------------------------------------------ billing
    def pay_in_app(self, session_id: str, now: float) -> list[Note]:
        """Retry after a failed or abandoned exit payment, from the phone."""
        sess = self._get(session_id)
        if sess.state not in ("confirming", "paying") or self._open_picks_for(session_id):
            raise EngineError(409, "Nothing to pay for yet")
        return self._complete(sess, now, how="in_app", clean=False)

    def _complete(self, sess: Session, now: float, how: str, clean: bool) -> list[Note]:
        """Bill the trip. Sandbox charges at once; a checkout gateway (Razorpay) asks the phone to
        approve, and the trip finishes in payment_confirmed()."""
        sess.pending = {"how": how, "clean": clean}
        self._reprice(sess)
        if sess.total == 0:
            return self._finalize(sess, now, {"payment_id": None, "status": "captured", "amount": 0,
                                              "method": "nothing to pay", "mode": self.payments.mode,
                                              "provider": self.payments.name})
        if self.payments.flow == "checkout":
            return self._request_payment(sess, now)
        if self.payments.flow == "mandate":
            return self._charge_mandate(sess, now)
        result = self.payments.charge(sess.session_id, sess.total)
        if result["status"] != "captured":
            return self._payment_failed(sess, now, "Your payment didn't go through. Please pay in the app.")
        return self._finalize(sess, now, result)

    def _payment_failed(self, sess: Session, now: float, text: str) -> list[Note]:
        metrics.PAYMENTS.labels("failed").inc()
        self.stats["payments_failed"] += 1
        sess.state = "paying" if self.payments.flow == "checkout" else "confirming"
        return [
            self._log(now, "payment.failed", f"Exit payment failed: {text}", sess.session_id, level="warn"),
            Note(f"phone:{sess.session_id}", "payment_failed", {"text": text}),
            Note(f"gate:{sess.gate_id}", "gate", {"light": "amber", "text": "Please pay at the staff desk"
                                                  if self.payments.flow == "mandate" else "Payment pending on the phone"}),
        ]

    # ------------------------------------------------------------------ UPI Reserve Pay (mandate)
    def link_mandate(self, shopper_id: str, shopper_secret: str, customer: dict, upi_id: str | None,
                     now: float) -> tuple[dict, list[Note]]:
        """Once per shopper: block funds with UPI Reserve Pay. The shopper approves in their UPI app;
        the gateway's webhook then marks the mandate ACTIVE."""
        from .payments import PaymentError
        if self.payments.flow != "mandate":
            raise EngineError(409, "This store isn't using UPI Reserve Pay")
        if not self.ledger.check_shopper(shopper_id, shopper_secret):
            raise EngineError(401, "Unknown shopper")
        try:
            m = self.payments.create_mandate(shopper_id, customer, upi_id, now)
        except PaymentError as err:
            raise EngineError(502, str(err)) from err
        self.ledger.save_mandate(m["subscription_id"], shopper_id, "PENDING", now, now + 89 * 86400)
        return m, [self._log(now, "mandate.requested", "Reserve Pay authorisation sent to the shopper's UPI app",
                             subscription=m["subscription_id"])]

    def mandate_status(self, shopper_id: str, shopper_secret: str, now: float) -> dict:
        if not self.ledger.check_shopper(shopper_id, shopper_secret):
            raise EngineError(401, "Unknown shopper")
        return self.ledger.mandate_for(shopper_id, now) or {"status": "NONE"}

    def _charge_mandate(self, sess: Session, now: float) -> list[Note]:
        from .payments import PaymentError
        mandate = self.ledger.mandate_for(sess.shopper_id, now)
        if not mandate or mandate["status"] != "ACTIVE":
            return self._payment_failed(sess, now, "UPI Reserve Pay isn't active for you yet. Please pay at the staff desk.")
        if sess.total > self.payments.max_amount_paise:
            return self._payment_failed(sess, now, "This basket is above your Reserve Pay limit. Please pay at the staff desk.")
        try:
            result = self.payments.charge(mandate["subscription_id"], sess.session_id, sess.total, now)
        except PaymentError as err:
            return self._payment_failed(sess, now, f"The Reserve Pay debit didn't go through ({err}). Please pay at the desk.")
        result["subscription_id"] = mandate["subscription_id"]
        return self._finalize(sess, now, result)

    def cashfree_webhook(self, event_type: str, body: dict, now: float) -> list[Note]:
        """Signature-checked Cashfree webhook: mandate approvals and debit results."""
        def find(d, key):
            if isinstance(d, dict):
                if key in d:
                    return d[key]
                for v in d.values():
                    found = find(v, key)
                    if found is not None:
                        return found
            return None

        sub_id = find(body, "subscription_id")
        if event_type == "SUBSCRIPTION_AUTH_STATUS":
            status = find(body, "subscription_status") or ("ACTIVE" if find(body, "payment_status") == "SUCCESS" else "FAILED")
            if sub_id and self.ledger.set_mandate_status(sub_id, status):
                return [self._log(now, "mandate.status", f"Reserve Pay mandate {sub_id} is {status}", subscription=sub_id)]
            return []
        if event_type == "SUBSCRIPTION_PAYMENT_SUCCESS":
            return [self._log(now, "mandate.debited", f"Reserve Pay debit {find(body, 'payment_id')} succeeded",
                              subscription=sub_id)]
        if event_type == "SUBSCRIPTION_PAYMENT_FAILED":
            pid = str(find(body, "payment_id") or "")
            sess = next((s for s in self.sessions.values() if s.receipt and s.receipt.get("payment_id") == pid), None)
            if sess:
                sess.receipt["gateway_status"] = "FAILED"
            return [self._log(now, "mandate.debit_failed",
                              f"Reserve Pay debit {pid} FAILED after the shopper left: collect from trip "
                              f"{sess.session_id if sess else 'unknown'} (follow up with the shopper)",
                              sess.session_id if sess else None, level="warn")]
        return []

    def _request_payment(self, sess: Session, now: float) -> list[Note]:
        from .payments import PaymentError
        order = sess.payment_order
        if order is None or order["amount"] != sess.total:  # reuse the order unless the basket changed
            try:
                order = self.payments.create_order(sess.session_id, sess.total)
            except PaymentError as err:
                return self._payment_failed(sess, now, f"Couldn't start the payment ({err}). Please try again.")
        sess.payment_order = order
        sess.state = "paying"
        return [
            self._log(now, "payment.requested", f"Asked shopper to approve Rs {sess.total / 100:.2f} "
                      f"({self.payments.name} order {order['id']})", sess.session_id),
            Note(f"phone:{sess.session_id}", "pay", {**self.payments.checkout_options(order),
                                                     "session_id": sess.session_id, "mode": self.payments.mode}),
            Note(f"gate:{sess.gate_id}", "gate", {"light": "amber", "text": "Approve the payment on your phone"}),
        ]

    def payment_confirmed(self, session_id: str, order_id: str, payment_id: str, signature: str,
                          now: float) -> list[Note]:
        """The phone reports Checkout success; trust it only after the gateway confirms."""
        from .payments import PaymentError
        sess = self._get(session_id)
        if sess.state == "exited":
            return []  # already finished, e.g. by the webhook
        if sess.state != "paying" or not sess.payment_order or sess.payment_order["id"] != order_id:
            raise EngineError(409, "No payment is pending for this order")
        try:
            result = self.payments.confirm(order_id, payment_id, signature)
        except PaymentError as err:
            raise EngineError(402, str(err), self._payment_failed(sess, now, "We couldn't confirm that payment. Please try again."))
        return self._finalize(sess, now, result)

    def payment_webhook(self, event: str, payment: dict, now: float) -> list[Note]:
        """Gateway webhook (already signature-checked): finishes a trip if the phone went away mid-payment."""
        sess = next((s for s in self.sessions.values()
                     if s.payment_order and s.payment_order["id"] == payment.get("order_id")), None)
        if not sess or sess.state == "exited":
            return []
        if event == "payment.captured":
            return self._finalize(sess, now, self.payments.receipt(payment))
        if event == "payment.failed":
            return self._payment_failed(sess, now, payment.get("error_description") or "The payment failed. Please try again.")
        return []

    def _restock(self, line: CartLine, direction: int) -> None:
        amount = (line.grams or 0) / 1000 if line.grams is not None else 1
        self.stock[line.sku] = round(self.stock.get(line.sku, 0) + direction * amount, 3)

    def _finalize(self, sess: Session, now: float, result: dict) -> list[Note]:
        how, clean = (sess.pending or {}).get("how", "auto"), (sess.pending or {}).get("clean", False)
        metrics.PAYMENTS.labels("captured").inc()
        self.stats["payments_ok"] += 1
        sess.state = "exited"
        for ln in sess.live_lines:
            self._restock(ln, -1)
        pricing = self._reprice(sess)
        invoice = self.invoicer.issue(sess, pricing, result, {p.sku: p for p in self.catalog.all()}, now)
        if pricing["points_redeemed"]:
            self.ledger.add_points(sess.shopper_id, -pricing["points_redeemed"])
        balance = self.ledger.add_points(sess.shopper_id, pricing["points_earned"])
        if pricing["coupon"] and pricing["coupon"]["applied"]:
            self.ledger.use_coupon(sess.shopper_id, pricing["coupon"]["code"], now)
        sess.receipt = {**result, "items": [ln.public() for ln in sess.live_lines],
                        "subtotal": pricing["subtotal"], "discounts": pricing["discounts"], "gst": pricing["gst"],
                        "lines_net": {pl["line_id"]: pl["net"] for pl in pricing["lines"]},
                        "points": {"earned": pricing["points_earned"], "redeemed": pricing["points_redeemed"],
                                   "balance": balance},
                        "invoice": invoice, "total": sess.total, "how": how, "ts": now}
        for listener in self.invoice_listeners:
            try:
                listener(sess, invoice)
            except Exception as err:  # a mail outage must never block the exit
                log.warning("Invoice listener failed: %s", err)
        streak = self.ledger.clean_streak(sess.shopper_id)
        self.ledger.set_clean_streak(sess.shopper_id, streak + 1 if clean else 0)
        self.checks.pop(sess.session_id, None)
        self._update_gauges(now)
        return [
            self._log(now, "exit.paid", f"Charged Rs {sess.total / 100:.2f} via {result.get('provider')} "
                      f"({result['payment_id']}), gate opened", sess.session_id, how=how,
                      items=[[ln.sku, ln.price, ln.grams] for ln in sess.live_lines], total=sess.total),
            Note(f"phone:{sess.session_id}", "receipt", sess.receipt),
            Note(f"gate:{sess.gate_id}", "gate", {"light": "green", "text": "Thank you! Gate open"}),
            self._cart_note(sess),
        ]

    def complete_offline_exit(self, session_id: str, gate_id: str, now: float) -> list[Note]:
        """Charge a trip the gateway let through while the cloud was unreachable."""
        sess = self._get(session_id)
        if sess.state in ("exited", "cancelled"):
            return []
        sess.gate_id = gate_id
        return self._complete(sess, now, how="offline", clean=False)

    # ------------------------------------------------------------------ monitoring
    def summary(self, now: float, cloud_online: bool, buffered: int = 0) -> dict:
        s = self.s
        due = [p for p in self.picks.values() if p.candidates and p.status != "returned"
               and (p.status != "open" or now - p.ts >= s.match_window_s)]
        matched_due = sum(1 for p in due if p.status == "matched")
        match_rate = matched_due / len(due) if due else None
        attributed = self.stats["picks"] - self.stats["unattributed"]
        lat = sorted(self.latencies)
        p95 = lat[int(0.95 * (len(lat) - 1))] if lat else None
        cleared_ratio = self.stats["red_cleared"] / self.stats["red_resolved"] if self.stats["red_resolved"] else None
        stations = [{**st, "age_s": round(now - st["last_seen"], 1),
                     "ok": now - st["last_seen"] <= s.station_silent_s} for st in self.stations.values()]
        open_due = [p for p in self.picks.values() if p.status == "open" and p.candidates and now - p.ts >= s.match_window_s]

        alerts: list[dict] = []

        def alert(aid: str, level: str, text: str) -> None:
            alerts.append({"id": aid, "level": level, "text": text})

        if match_rate is not None and len(due) >= 5 and match_rate < s.match_rate_alert:
            alert("match_rate", "warn", f"Pick-to-scan match rate {match_rate:.0%} is below {s.match_rate_alert:.0%}: "
                                        f"check camera angle and product data")
        recent_picks = sum(1 for t, k in self.recent if k == "pick" and now - t <= 900)
        recent_nudges = sum(1 for t, k in self.recent if k == "nudge" and now - t <= 900)
        overall = self.stats["nudges"] / attributed if attributed else 0
        if recent_picks >= 10 and attributed >= 20 and overall > 0 and recent_nudges / recent_picks > s.nudge_spike_factor * overall:
            alert("nudge_spike", "warn", f"Nudges in the last 15 min are {recent_nudges / recent_picks / overall:.1f}x the "
                                         f"usual rate: look for a wrong barcode or price in the catalog")
        for p in open_due:
            value = self._pick_value(p)
            if value >= s.high_value_paise:
                who = p.candidates[0] if len(p.candidates) == 1 else f"{len(p.candidates)} trips"
                alert(f"high_value:{p.pick_id}", "warn", f"Rs {value / 100:.0f} {self.catalog.get(p.sku).name} unscanned "
                                                         f"for {now - p.ts:.0f} s ({who}): watch this trip")
        exits = sum(self.stats[k] for k in ("green", "amber", "red"))
        if exits >= 20 and self.stats["red"] / exits > s.red_exit_alert:
            alert("red_exits", "warn", f"{self.stats['red'] / exits:.0%} of exits are red (alert above {s.red_exit_alert:.0%}): "
                                       f"review risk thresholds")
        if cleared_ratio is not None and self.stats["red_resolved"] >= 3 and cleared_ratio > s.red_cleared_alert:
            alert("red_cleared", "warn", f"Staff found nothing wrong in {cleared_ratio:.0%} of red checks: "
                                         f"thresholds may be too strict")
        for st in stations:
            sid = st["station_id"]
            if not st["ok"]:
                alert(f"station:{sid}", "bad", f"Shelf station {sid} silent for {st['age_s']:.0f} s: shelf {st['shelf_id']} is unverified")
                continue
            for lane in st.get("lanes", []) or []:
                if abs(lane.get("drift_g", 0)) > s.scale_drift_alert_g:
                    alert(f"drift:{sid}:{lane.get('lane')}", "warn",
                          f"Scale drift {lane['drift_g']:+.1f} g on {sid} lane {lane.get('lane')}"
                          + (" (re-zeroed automatically)" if lane.get("rezeroed") else ": re-zero, then open a maintenance ticket"))
            fps = st.get("camera_fps")
            if fps is not None and fps < s.camera_fps_alert:
                alert(f"fps:{sid}", "warn", f"Camera on {sid} running at {fps:.1f} fps: product recognition may miss picks")
            battery = st.get("battery_pct")
            if battery is not None and battery < 20:
                alert(f"battery:{sid}", "warn", f"{sid} battery at {battery:.0f}%")
            if (st.get("outbox") or 0) > 50:
                alert(f"outbox:{sid}", "warn", f"{sid} has {st['outbox']} events waiting to send")
        if p95 is not None and p95 > s.latency_p95_alert_s:
            alert("latency", "bad", f"Shelf-to-server latency p95 {p95:.2f} s is above {s.latency_p95_alert_s} s")
        payments = self.stats["payments_ok"] + self.stats["payments_failed"]
        if payments >= 20 and self.stats["payments_ok"] / payments < s.payment_success_alert:
            alert("payments", "bad", f"Exit payment success {self.stats['payments_ok'] / payments:.1%} is below "
                                     f"{s.payment_success_alert:.0%}: shoppers are falling back to paying in the app")
        if buffered > s.offline_buffer_alert:
            alert("buffer", "bad", f"{buffered} events queued at the gateway (alert above {s.offline_buffer_alert})")
        vision = {k: self.stats.get(f"vision_{k}", 0) for k in ("agree", "override", "disagree", "low_confidence", "none")}
        judged = vision["agree"] + vision["override"] + vision["disagree"]
        vision["agreement_rate"] = vision["agree"] / judged if judged else None
        if judged >= 10 and 1 - vision["agreement_rate"] > s.vision_disagree_alert:
            alert("vision", "warn", f"Camera and weight disagree on {1 - vision['agreement_rate']:.0%} of picks: "
                                    f"check shelf layout, camera angle, or retrain the model")
        for sku, entry in self.learned.items():
            product = self.catalog.get(sku)
            if product and entry["n"] >= 10 and abs(entry["mean"] - product.unit_weight_g) / product.unit_weight_g > s.weight_learning_drift:
                alert(f"weight:{sku}", "info", f"{product.name} weighs {entry['mean']:.0f} g in practice "
                                               f"(catalog says {product.unit_weight_g:.0f} g): update the packaging data")
        if self.stats["silent_attributions"]:
            alert("silent", "info", f"{self.stats['silent_attributions']} shelf picks had no located shopper and were "
                                    f"assigned to shoppers with location off")
        if self.stats["unattributed"]:
            alert("unattributed", "info", f"{self.stats['unattributed']} shelf picks happened with no shopper in the store")
        if not cloud_online:
            alert("cloud", "bad", "Cloud unreachable: gateway is buffering events and checking tokens offline")
        gates = [{**g, "age_s": round(now - g["last_seen"], 1), "ok": now - g["last_seen"] <= 60} for g in self.gates.values()]
        for g in gates:
            gid = g["gate_id"]
            if not g["ok"]:
                alert(f"gate:{gid}", "bad", f"Gate agent {gid} silent for {g['age_s']:.0f} s: check the gate's Pi and turnstile")
                continue
            if g.get("camera") is False:
                alert(f"gate_camera:{gid}", "warn", f"Gate {gid}: QR camera not working, shoppers must tap by NFC")
            if (g.get("offline_queue") or 0) > 0:
                alert(f"gate_queue:{gid}", "warn", f"Gate {gid} let {g['offline_queue']} trip(s) out offline: not billed yet")
        edges = [{**e, "age_s": round(now - e["last_seen"], 1), "ok": now - e["last_seen"] <= 120} for e in self.edges.values()]
        for e in edges:
            eid = e["edge_id"]
            if not e["ok"]:
                alert(f"edge:{eid}", "bad", f"Store edge {eid} silent for {e['age_s']:.0f} s: shelf events from that store aren't arriving")
            elif (e.get("outbox") or 0) > 200:
                alert(f"edge_backlog:{eid}", "warn", f"Store edge {eid} has {e['outbox']} shelf messages waiting")

        active = [x for x in self.sessions.values() if x.state not in ("exited", "cancelled")]
        return {
            "now": now,
            "cloud_online": cloud_online,
            "match_window_s": s.match_window_s,
            "active_shoppers": len(active),
            "match_rate": match_rate,
            "nudge_rate": self.stats["nudges"] / attributed if attributed else None,
            "open_discrepancies": len(open_due),
            "exits": {k: self.stats[k] for k in ("green", "amber", "red")},
            "red_cleared_ratio": cleared_ratio,
            "latency_p95_s": p95,
            "payments": {"ok": self.stats["payments_ok"], "failed": self.stats["payments_failed"],
                         **self.payments.public_config()},
            "vision": vision,
            "presence_active": self.presence_active(now),
            "stats": self.stats,
            "stations": stations,
            "gates": gates,
            "edges": edges,
            "alerts": alerts,
            "checks": list(self.checks.values()),
            "disputes": [d for d in self.disputes.values() if d["status"] == "open"],
            "id_needed": [x.session_id for x in active if x.needs_id and not x.age_verified],
            "feed": list(self.feed)[:60],
            "sessions": [{"session_id": x.session_id, "state": x.state, "aisle": x.aisle,
                          "detected": x.presence_aisle if self._presence_fresh(x, now) else None,
                          "location_age_s": round(now - x.aisle_ts) if x.aisle else None,
                          "total": x.total, "risk": self.assess(x.session_id)["points"],
                          "needs_id": x.needs_id and not x.age_verified} for x in active],
            "learned_weights": {k: {"mean": round(v["mean"], 1), "n": v["n"]} for k, v in self.learned.items()},
            "ledger_events": self.ledger.count(),
        }
