"""Fusion and risk engine.

Matches shelf picks (camera + weight) to phone scans, keeps each shopper's virtual
cart, scores the trip, and decides at the exit gate: green, amber or red.

The engine is plain synchronous Python. Every method takes `now` so tests can drive
time, and returns a list of Notes that the web layer pushes over WebSockets.
`snapshot()` / `restore()` let the server save live trips and survive a restart.
"""
from __future__ import annotations

import json
import logging
import secrets
from collections import deque
from dataclasses import asdict, dataclass, field

from . import metrics
from .catalog import Catalog, Product
from .config import Settings
from .ledger import Ledger
from .security import (TokenSigner, cart_hash, eid_matches, new_session_id, new_session_key,
                       verify_scan_signature)

WEIGHT_RESIDUE_G = 20  # a loose-produce pick with less than this left counts as fully put back
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

    @property
    def total(self) -> int:
        return sum(ln.price for ln in self.lines if ln.status not in ("removed", "refunded"))

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


def _rebuild(cls, data: dict):
    return cls(**data)


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
            d["lines"] = [_rebuild(CartLine, x) for x in d.get("lines", [])]
            d["risk"] = [_rebuild(RiskEvent, x) for x in d.get("risk", [])]
            d["pending_scans"] = [_rebuild(PendingScan, x) for x in d.get("pending_scans", [])]
            self.sessions[d["session_id"]] = Session(**d)
        self.picks = {d["pick_id"]: Pick(**d) for d in data.get("picks", [])}
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
            raise EngineError(404, "No such shopping session")
        return sess

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
        sess.aisle, sess.aisle_ts = aisle, now
        notes = [self._log(now, "session.location", f"Phone reports aisle {aisle}", session_id)] if changed else []
        return notes + self._check_mismatch(sess, now)

    def _presence_fresh(self, sess: Session, now: float) -> bool:
        return bool(sess.presence_ts) and now - sess.presence_ts <= self.s.presence_fresh_s

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
                moved = sess.presence_aisle != aisle
                sess.presence_aisle, sess.presence_shelf, sess.presence_ts = aisle, shelf_id, now
                if moved:
                    notes.append(self._log(now, "session.presence", f"Detected near shelf {shelf_id} (aisle {aisle})",
                                           sess.session_id))
                notes += self._check_mismatch(sess, now)
        return notes

    def _check_mismatch(self, sess: Session, now: float) -> list[Note]:
        """The phone claims one aisle while a shelf detected it in another: likely spoofed location."""
        if not (self._presence_fresh(sess, now) and sess.aisle and now - sess.aisle_ts <= self.s.location_fresh_s):
            return []
        if sess.aisle == sess.presence_aisle or now - sess.mismatch_ts < 120:
            return []
        sess.mismatch_ts = now
        return self._add_risk(sess, now, "location_mismatch", self.s.location_mismatch_points,
                              f"Phone reported aisle {sess.aisle}, but shelf {sess.presence_shelf} "
                              f"detected it in aisle {sess.presence_aisle}")

    def cart_view(self, session_id: str) -> dict:
        sess = self._get(session_id)
        return {
            "session_id": sess.session_id,
            "state": sess.state,
            "aisle": sess.aisle,
            "lines": [ln.public() for ln in sess.lines],
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

        if package_label and package_label != product.sku and (package_confidence or 0) >= self.s.package_min_confidence:
            held = self.catalog.get(package_label)
            held_name = held.name if held else package_label
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

        pick = next((p for p in self._open_picks_for(session_id) if p.sku == product.sku), None)
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
        """Who could have taken this item, best evidence first:
        1. phones a shelf station detected in this aisle (observed, hard to fake),
        2. phones that reported this aisle,
        3. nobody located here: every shopper whose location we can't vouch for. Going silent, or
           switching Bluetooth off while stations are listening, makes you a suspect, not invisible."""
        shopping = [s for s in self.sessions.values() if s.state == "shopping"]
        how = "presence"
        cands = [s for s in shopping if self._presence_fresh(s, now) and s.presence_aisle == product.aisle]
        if not cands:
            how = "aisle"
            cands = [s for s in shopping if s.aisle == product.aisle and now - s.aisle_ts <= self.s.attribution_window_s
                     and not (self._presence_fresh(s, now) and s.presence_aisle != product.aisle)]
        if not cands:
            how = "silent"
            if self.presence_active(now):
                cands = [s for s in shopping if not self._presence_fresh(s, now)]
            else:
                cands = [s for s in shopping if s.aisle is None or now - s.aisle_ts > self.s.location_fresh_s]
        ids = [s.session_id for s in cands]
        # A shopper who already scanned this product counts too, whatever their location says.
        for s in shopping:
            if s.session_id not in ids and any(
                    ps.sku == product.sku and now - ps.ts <= self.s.match_window_s for ps in s.pending_scans):
                ids.append(s.session_id)
                how = how if cands else "scan"
        return ids, how

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
        if how == "silent":
            self.stats["silent_attributions"] += 1
            for sid in cands:
                notes += self._add_risk(self.sessions[sid], now, "silent_location",
                                        self.s.silent_location_points / len(cands),
                                        f"Location unknown while {product.name} was taken in aisle {product.aisle} "
                                        f"with no located shopper nearby", pick_id=pick.pick_id)
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

    def _return(self, product: Product, shelf_id: str, now: float, grams: float | None = None) -> list[Note]:
        self.stats["returns"] += 1
        metrics.RETURNS.inc()
        open_picks = sorted((p for p in self.picks.values()
                             if p.status == "open" and p.sku == product.sku and p.shelf_id == shelf_id),
                            key=lambda p: p.ts)
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
        matched.sort(key=lambda p: (self.sessions[p.matched_session].aisle == product.aisle, p.ts), reverse=True)
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
        amount = sum(ln.price for ln in lines)
        if not lines or amount <= 0:
            raise EngineError(400, "Choose at least one paid item to refund")
        provider = sess.receipt.get("provider")
        if provider in ("desk", None) or not sess.receipt.get("payment_id"):
            result = {"refund_id": f"desk_rfnd_{secrets.token_hex(5)}", "amount": amount, "status": "processed",
                      "provider": "desk"}
        else:
            from .payments import PaymentError
            try:
                result = self.payments.refund(sess.receipt["payment_id"], amount)
            except PaymentError as err:
                raise EngineError(502, f"Refund failed: {err}") from err
        for ln in lines:
            ln.status, ln.note = "refunded", reason or "Refunded"
            self._restock(ln, +1)
        sess.receipt.setdefault("refunds", []).append({**result, "lines": line_ids, "reason": reason, "ts": now})
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
        if sess.total == 0:
            return self._finalize(sess, now, {"payment_id": None, "status": "captured", "amount": 0,
                                              "method": "nothing to pay", "mode": self.payments.mode,
                                              "provider": self.payments.name})
        if self.payments.flow == "checkout":
            return self._request_payment(sess, now)
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
            Note(f"gate:{sess.gate_id}", "gate", {"light": "amber", "text": "Payment pending on the phone"}),
        ]

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
        sess.receipt = {**result, "items": [ln.public() for ln in sess.live_lines],
                        "total": sess.total, "how": how, "ts": now}
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
