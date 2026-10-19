"""Accuracy simulation: how often the real decision engine bothers honest shoppers, and how much theft
it catches, with many shoppers in the store at once.

It drives server.engine.Engine directly (no HTTP) on a simulated clock. Each shopper walks aisles,
takes items (which fires the shelf's take event), scans or doesn't, may put items back, reacts (or
not) to nudges, and exits. At the exit the shopper answers amber prompts truthfully (honest) or denies
(thieves), and staff checks resolve to the truth. Ground truth is known for every item, so we count:

  honest trips:  green / amber (a confirm tap) / red (a staff stop), and any overcharge
  theft trips:   caught (item ends up paid, or a staff stop) vs. walked out with it

Shopper mix (default): careful 70%, forgetful 20%, pocketing thief 6%, barcode-swap thief 4%.
Phones: 50% run the native app (Bluetooth presence), 8% go silent (no location updates).
These are assumptions, not measurements: replace them with pilot data when you have it.

    python -m tools.simulate                          # 2000 trips, current settings, report
    python -m tools.simulate --sweep                  # also try other green/amber thresholds
    python -m tools.simulate --trips 3000 --check     # CI: fail if the rules regress
"""
from __future__ import annotations

import argparse
import heapq
import itertools
import json
import random
import statistics
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from server.catalog import Catalog
from server.config import ROOT, Settings
from server.engine import Engine, EngineError
from server.ledger import Ledger
from server.payments import SandboxPayments
from server.security import TokenSigner, eid_for, sign_scan, verify_token

T0 = 1_800_000_000.0


def store_layout(copies: int) -> Catalog:
    """The demo catalog spread over `copies` times as many aisles (aisle 7 -> 7A, 7B, ...), so a simulated
    store has many aisles like a real one. copies=1 is the tiny demo store, where everyone crowds aisle 7."""
    from dataclasses import replace
    base = Catalog.load(ROOT / "data" / "catalog.json").all()
    if copies == 1:
        return Catalog(base)
    out = []
    for p in base:
        for i in range(copies):
            aisle = f"{p.aisle}{'ABCDEFGH'[i]}"
            out.append(replace(p, sku=f"{p.sku}-{i}", barcode=str(int(p.barcode) + 1000 * i) if len(p.barcode) > 5
                               else f"{p.barcode}{i}", aisle=aisle,
                               shelf_id=f"S{aisle}" if p.shelf_id else None))
    return Catalog(out)


@dataclass
class Taken:
    sku: str
    pick_time: float
    scanned: bool = False
    returned: bool = False
    swap_barcode: str | None = None
    scan_pending: bool = False


@dataclass
class Shopper:
    kind: str                # careful | forgetful | pocket | swap
    native: bool             # Bluetooth presence via the native app
    silent: bool             # phone stops reporting its aisle
    sid: str = ""
    key: str = ""
    seq: int = 0
    taken: list[Taken] = field(default_factory=list)
    first_decision: str = ""
    final: str = ""
    reasons: list = field(default_factory=list)
    where: str | None = None

    @property
    def thief(self) -> bool:
        return self.kind in ("pocket", "swap")


class Sim:
    def __init__(self, settings: Settings, seed: int, trips: int, mix: dict, native_share: float, silent_share: float,
                 copies: int = 4):
        self.rng = random.Random(seed)
        self.tmp = tempfile.TemporaryDirectory()
        settings.data_dir = Path(self.tmp.name)
        settings.offers_path = Path(self.tmp.name) / "no-offers.json"
        self.engine = Engine(settings, store_layout(copies), Ledger(":memory:"),
                             TokenSigner(Path(self.tmp.name) / "k.pem"), SandboxPayments())
        # ID checks are policy, not risk; loose produce is weighed at the shelf (tested separately).
        self.products = [p for p in self.engine.catalog.all() if not p.age_restricted and not p.by_weight]
        self.monitored = [p for p in self.products if p.monitored and not p.by_weight]
        self.valuable = [p for p in self.monitored if p.theft != "low"]
        self.trips, self.mix = trips, mix
        self.native_share, self.silent_share = native_share, silent_share
        self.app_putback_share = 0.7
        self.thief_denies = 0.5
        self.queue: list = []
        self.counter = itertools.count()
        self.shoppers: list[Shopper] = []

    def at(self, t: float, fn, *args) -> None:
        heapq.heappush(self.queue, (t, next(self.counter), fn, args))

    # ---------------------------------------------------------------- shopper behaviour
    def arrive(self, now: float, sh: Shopper) -> None:
        sess, _, _ = self.engine.start_session(None, None, True, now)
        sh.sid, sh.key = sess.session_id, sess.key
        self.at(now + 15, self.heartbeat, sh)
        t = now + self.rng.uniform(10, 30)
        n_items = self.rng.randint(1, 5)
        plan = [self.rng.choice(self.products) for _ in range(n_items)]
        if sh.kind == "pocket":
            plan.append(("POCKET", self.rng.choice(self.valuable)))
        elif sh.kind == "swap":
            plan.append(("SWAP", self.rng.choice(self.valuable)))
        self.rng.shuffle(plan)
        for item in plan:
            mode, product = item if isinstance(item, tuple) else ("BUY", item)
            self.at(t, self.walk_to, sh, product.aisle)
            t += self.rng.uniform(15, 60)
            self.at(t, self.pick, sh, product, mode)
            t += self.rng.uniform(20, 90)
        self.at(t + self.rng.uniform(20, 60), self.exit, sh)

    def heartbeat(self, now: float, sh: Shopper) -> None:
        sess = self.engine.sessions[sh.sid]
        if sess.state != "shopping":
            return
        if sh.where:
            self.walk_to(now, sh, sh.where)
        self.at(now + 15, self.heartbeat, sh)

    def walk_to(self, now: float, sh: Shopper, aisle: str) -> None:
        sess = self.engine.sessions[sh.sid]
        if sess.state != "shopping":
            return
        sh.where = aisle
        if not sh.silent:
            self.engine.set_location(sh.sid, aisle, now)
        if sh.native:  # the aisle's shelf stations hear the phone's rotating ID
            shelf = next((p.shelf_id for p in self.monitored if p.aisle == aisle), None)
            if shelf:
                self.engine.presence(f"st-{shelf}", shelf, [eid_for(sh.key, now)], now)

    def pick(self, now: float, sh: Shopper, product, mode: str) -> None:
        if self.engine.sessions[sh.sid].state != "shopping":
            return
        took = Taken(product.sku, now)
        sh.taken.append(took)
        if product.monitored:
            grams = product.unit_weight_g * self.rng.uniform(3, 8) if product.by_weight else None
            self.engine.shelf_event(f"st-{product.shelf_id}", product.shelf_id, "take", now, lane=product.lane,
                                    weight_delta_g=-grams if grams else None)
        if mode == "POCKET":
            return
        if mode == "SWAP":
            cheap = min(self.products, key=lambda p: p.price)
            took.swap_barcode = cheap.barcode
            self.at(now + self.rng.uniform(3, 15), self.scan, sh, cheap.barcode, None)
            return
        forget = sh.kind == "forgetful" and self.rng.random() < 0.3
        if not forget:
            took.scan_pending = True
            self.at(now + self.rng.uniform(3, 25), self.scan, sh, product.barcode, took)
        if self.rng.random() < 0.08:  # changes their mind
            self.at(now + self.rng.uniform(40, 120), self.put_back, sh, product, took)

    def scan(self, now: float, sh: Shopper, barcode: str, took: Taken | None) -> None:
        if self.engine.sessions[sh.sid].state != "shopping":
            return
        if took and took.returned:
            return
        sh.seq += 1
        ts = int(now * 1000)
        try:
            self.engine.scan(sh.sid, sh.seq, barcode, ts, sign_scan(sh.key, sh.sid, sh.seq, barcode, ts), now)
            if took:
                took.scanned = True
        except EngineError:
            pass

    def not_me(self, now: float, sh: Shopper, pick_id: str) -> None:
        try:
            self.engine.not_me(sh.sid, pick_id, now)
        except EngineError:
            pass

    def put_back(self, now: float, sh: Shopper, product, took: Taken) -> None:
        sess = self.engine.sessions[sh.sid]
        if sess.state != "shopping" or took.returned:
            return
        took.returned = True
        line = next((ln for ln in reversed(sess.live_lines) if ln.sku == product.sku and not ln.return_ts), None)
        if line and (not product.monitored or self.rng.random() < self.app_putback_share):
            self.engine.put_back(sh.sid, line.line_id, now)  # taps "Put back" in the app first
        if product.monitored:
            self.walk_to(now, sh, product.aisle)  # back at the shelf
            self.engine.shelf_event(f"st-{product.shelf_id}", product.shelf_id, "return", now + 5, lane=product.lane)

    def on_notes(self, now: float, notes) -> None:
        """Phones react to nudges: honest shoppers usually scan the item; thieves ignore it."""
        for note in notes:
            if note.kind != "nudge" or not note.channel.startswith("phone:"):
                continue
            sh = self.by_sid.get(note.channel.split(":", 1)[1])
            sku = note.data.get("sku")
            if not sh or not sku:
                continue
            has_unscanned = any(t.sku == sku and not t.scanned and not t.returned for t in sh.taken)
            pick_id = note.data.get("pick_id")
            # "Not me": honest shoppers usually answer when they didn't take it; thieves sometimes lie.
            lie = sh.thief and self.rng.random() < self.thief_denies
            if pick_id and ((not has_unscanned and not sh.thief and self.rng.random() < 0.8) or lie):
                self.at(now + self.rng.uniform(5, 30), self.not_me, sh, pick_id)
                continue
            if sh.thief:
                continue
            took = next((t for t in sh.taken if t.sku == sku and not t.scanned and not t.returned
                         and not t.scan_pending), None)
            if took and self.rng.random() < 0.85:
                product = self.engine.catalog.get(sku)
                took.scan_pending = True
                self.at(now + self.rng.uniform(5, 40), self.scan, sh, product.barcode, took)

    def exit(self, now: float, sh: Shopper) -> None:
        eng = self.engine
        sess = eng.sessions[sh.sid]
        token, _ = eng.checkout(sh.sid, now)
        sh.first_decision = eng.assess(sh.sid)["decision"]
        eng.gate_tap("G1", verify_token(token, eng.signer.public_key, now), now)
        sh.reasons = eng.assess(sh.sid)["reasons"]
        if sess.state == "confirming":
            unresolved = eng.assess(sh.sid)["unresolved"]
            take = [] if sh.thief else self.missing_from_cart(sh, unresolved)
            deny = [u["pick_id"] for u in unresolved if u["pick_id"] not in take]
            eng.confirm(sh.sid, take, deny, now + 5)
        for _ in range(2):  # an ID check can lead into a risk check
            if sess.state != "staff_check":
                break
            # Staff look in the basket: they find what was really taken and not yet in the cart.
            check = eng.checks.get(sh.sid, {})
            if check.get("kind") == "id":
                eng.staff_resolve(sh.sid, "id_verified", [], now + 60)
                continue
            found = self.missing_from_cart(sh, check.get("unresolved", []))
            eng.staff_resolve(sh.sid, "items_added" if found else "cleared", found, now + 60)
        sh.final = sess.state

    def missing_from_cart(self, sh: Shopper, unresolved: list[dict]) -> list[str]:
        """Picks to add: one per unit the shopper really has that isn't in the cart yet."""
        sess = self.engine.sessions[sh.sid]
        out = []
        for u in unresolved:
            have = sum(1 for t in sh.taken if t.sku == u["sku"] and not t.returned)
            in_cart = sum(1 for ln in sess.live_lines if ln.sku == u["sku"]) + sum(
                1 for pid in out if self.engine.picks[pid].sku == u["sku"])
            if have > in_cart:
                out.append(u["pick_id"])
        return out

    # ---------------------------------------------------------------- run
    def run(self) -> dict:
        kinds = list(self.mix)
        weights = [self.mix[k] for k in kinds]
        t = T0
        for _ in range(self.trips):
            sh = Shopper(self.rng.choices(kinds, weights)[0], self.rng.random() < self.native_share,
                         self.rng.random() < self.silent_share)
            self.shoppers.append(sh)
            self.at(t, self.arrive, sh)
            t += self.rng.expovariate(1 / 25)  # a new shopper every ~25 s: 15-30 in the store at once
        self.by_sid: dict[str, Shopper] = {}
        last_tick = T0
        concurrency = []
        while self.queue:
            now, _, fn, args = heapq.heappop(self.queue)
            while last_tick + 1 <= now:  # the server's 1-second ticker: nudges
                last_tick += 1
                self.on_notes(last_tick, self.engine.tick(last_tick))
            fn(now, *args)
            if fn == self.arrive:
                self.by_sid[args[0].sid] = args[0]
                concurrency.append(sum(1 for s in self.engine.sessions.values() if s.state == "shopping"))
        return self.report(concurrency)

    def report(self, concurrency: list[int]) -> dict:
        def paid_skus(sh: Shopper) -> list[str]:
            receipt = self.engine.sessions[sh.sid].receipt or {}
            return [i["sku"] for i in receipt.get("items", [])]

        honest = [s for s in self.shoppers if not s.thief]
        thieves = [s for s in self.shoppers if s.thief]
        overcharged = 0
        for sh in honest:
            paid = paid_skus(sh)
            truly = [t.sku for t in sh.taken if not t.returned]
            for sku in set(paid):
                if paid.count(sku) > truly.count(sku):
                    overcharged += 1
                    break
        caught = 0
        loss_paise = 0
        for sh in thieves:
            paid = paid_skus(sh)
            stolen = [t for t in sh.taken if not t.returned and (t.swap_barcode or not t.scanned)]
            unpaid = [t for t in stolen if paid.count(t.sku) < sum(1 for x in sh.taken if x.sku == t.sku and not x.returned)]
            if sh.first_decision == "red" or not unpaid:
                caught += 1
            loss_paise += sum(self.engine.catalog.get(t.sku).price for t in unpaid)

        def share(group, decision):
            return round(sum(1 for s in group if s.first_decision == decision) / max(1, len(group)), 4)

        forgetful = [s for s in honest if s.kind == "forgetful"]
        picks = [p for p in self.engine.picks.values() if p.candidates or p.matched_session]
        shared = sum(1 for p in picks if len(p.candidates) > 1) / max(1, len(picks))
        return {
            "picks_shared": round(shared, 4),
            "trips": len(self.shoppers), "honest": len(honest), "thieves": len(thieves),
            "concurrent_shoppers": {"median": statistics.median(concurrency) if concurrency else 0,
                                    "max": max(concurrency) if concurrency else 0},
            "honest_green": share(honest, "green"), "honest_amber": share(honest, "amber"), "honest_red": share(honest, "red"),
            "forgetful_green": share(forgetful, "green"),
            "honest_overcharged": overcharged,
            "theft_caught": round(caught / max(1, len(thieves)), 4),
            "theft_loss_rupees": round(loss_paise / 100, 2),
            "thieves_red": share(thieves, "red"),
            "honest_red_reasons": _top_reasons([s for s in honest if s.first_decision == "red"]),
            "honest_amber_reasons": _top_reasons([s for s in honest if s.first_decision == "amber"]),
        }


def _top_reasons(group: list[Shopper], n: int = 4) -> list[tuple[str, int]]:
    import re
    counts: dict[str, int] = {}
    for sh in group:
        for r in sh.reasons:
            key = re.sub(r"T[0-9A-F]{10}|\d+(\.\d+)?", "#", r)[:90]
            counts[key] = counts.get(key, 0) + 1
    return sorted(counts.items(), key=lambda kv: -kv[1])[:n]


def simulate(trips: int, seed: int, green_max: float | None = None, amber_max: float | None = None,
             copies: int = 4) -> dict:
    s = Settings()
    s.match_window_s = 60
    if green_max is not None:
        s.green_max = green_max
    if amber_max is not None:
        s.amber_max = amber_max
    mix = {"careful": 0.70, "forgetful": 0.20, "pocket": 0.06, "swap": 0.04}
    result = Sim(s, seed, trips, mix, native_share=0.5, silent_share=0.08, copies=copies).run()
    return {"green_max": s.green_max, "amber_max": s.amber_max, "aisles_with_shelves": 4 * copies, **result}


def to_markdown(base: dict, sweep: list[dict], layouts: list[dict] | None = None) -> str:
    pct = lambda v: f"{v * 100:.1f}%"  # noqa: E731
    lines = [
        "# TrustLane accuracy simulation", "",
        f"{base['trips']} trips ({base['honest']} honest, {base['thieves']} thieves); "
        f"{base['concurrent_shoppers']['median']:.0f} shoppers in the store at once (max {base['concurrent_shoppers']['max']}), "
        f"{base['aisles_with_shelves']} aisles with shelf stations; {pct(base['picks_shared'])} of shelf picks had more than "
        "one possible taker.",
        "Shopper behaviour is assumed, not measured: replace the mix with pilot data.", "",
        "| Thresholds (green / amber) | Honest green | Honest amber (one tap) | Honest red (staff stop) | Forgetful green | Overcharged | Theft caught | Theft loss |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in [base] + sweep:
        lines.append(f"| {r['green_max']:.0f} / {r['amber_max']:.0f}{' (current)' if r is base else ''} | {pct(r['honest_green'])} | "
                     f"{pct(r['honest_amber'])} | {pct(r['honest_red'])} | {pct(r['forgetful_green'])} | {r['honest_overcharged']} | "
                     f"{pct(r['theft_caught'])} | Rs {r['theft_loss_rupees']:,.0f} |")
    lines += ["", "*Caught* = the stolen item ended up paid, or the trip was stopped for a staff check. "
              "Forgetful shoppers who fixed a nudge in time still go green; the rest answer one amber prompt."]
    if layouts:
        lines += ["", "## Crowding", "",
                  "The shelf knows *what* was taken, and Bluetooth/aisle reports know *who was nearby*. When several "
                  "shoppers stand at the same shelf, a pocketed item can't be pinned on one of them without tracking "
                  "people on camera, which this design avoids. Catch rate therefore depends on how crowded the "
                  "monitored shelves are:", "",
                  "| Aisles with shelf stations | Picks shared | Honest red | Theft caught |", "|---|---|---|---|"]
        for r in layouts:
            lines.append(f"| {r['aisles_with_shelves']} | {pct(r['picks_shared'])} | {pct(r['honest_red'])} | {pct(r['theft_caught'])} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description="TrustLane accuracy simulation")
    ap.add_argument("--trips", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--sweep", action="store_true", help="also try other green/amber thresholds")
    ap.add_argument("--check", action="store_true", help="exit non-zero if the rules regress (CI)")
    ap.add_argument("--out", default=str(ROOT / "reports"))
    args = ap.parse_args()

    base = simulate(args.trips, args.seed)
    layouts = [] if args.check else [simulate(args.trips, args.seed, copies=c) for c in (1, 2)] + [base]
    sweep = []
    if args.sweep:
        for g, a in ((15, 45), (25, 45), (35, 60), (25, 80), (40, 80)):
            sweep.append(simulate(args.trips, args.seed, g, a))
    out = Path(args.out)
    out.mkdir(exist_ok=True)
    (out / "accuracy.json").write_text(json.dumps({"current": base, "sweep": sweep, "layouts": layouts}, indent=2),
                                       encoding="utf-8")
    (out / "accuracy.md").write_text(to_markdown(base, sweep, layouts), encoding="utf-8")
    print(to_markdown(base, sweep, layouts))
    if args.check:
        # Regression guards at the realistic layout (16 monitored aisles), not targets: see reports/accuracy.md.
        problems = []
        if base["honest_red"] > 0.04:
            problems.append(f"honest shoppers stopped by staff {base['honest_red']:.1%} > 4%")
        if base["honest_green"] < 0.85:
            problems.append(f"honest shoppers waved through {base['honest_green']:.1%} < 85%")
        if base["honest_overcharged"] > 0.015 * base["honest"]:
            problems.append(f"{base['honest_overcharged']} honest shoppers overcharged (> 1.5%)")
        if base["theft_caught"] < 0.50:
            problems.append(f"theft caught {base['theft_caught']:.1%} < 50%")
        if problems:
            raise SystemExit("Accuracy check failed: " + "; ".join(problems))
        print("Accuracy check passed.")


if __name__ == "__main__":
    main()
