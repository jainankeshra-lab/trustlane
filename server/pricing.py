"""Pricing: offers, coupons, loyalty points and GST for one basket.

Shelf prices are MRP-style, GST included. For a basket this module works out, in order:

  1. line offers, each line used by at most one offer:
       buy_x_get_y   buy 2 gum, get 1 free (the cheapest units go free)
       bundle        milk + bread for Rs 110
       percent       10% off chocolate
  2. basket offers on what's left (Rs 50 off above Rs 500), then one coupon the shopper applied
  3. loyalty points the shopper chose to redeem
  4. GST: every discount is spread over the lines it applies to (basket-wide discounts in proportion
     to each line's value), so tax is charged on what the shopper actually pays. Intra-state sale:
     the tax is split equally into CGST and SGST. Goods outside GST (e.g. beer, under state excise)
     are listed separately.

The result is plain data (paise), used for the phone's cart, the exit charge and the invoice.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Offers:
    promotions: list[dict] = field(default_factory=list)
    coupons: dict[str, dict] = field(default_factory=dict)  # CODE -> rule
    loyalty: dict = field(default_factory=lambda: {"earn_per_100_rupees": 1, "point_value_paise": 100, "min_redeem": 50})

    @classmethod
    def load(cls, path: Path | str) -> "Offers":
        path = Path(path)
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8"))
        validate(raw)
        return cls(raw.get("promotions", []), {c["code"].upper(): c for c in raw.get("coupons", [])},
                   raw.get("loyalty", cls().loyalty))

    def to_json(self) -> dict:
        return {"promotions": self.promotions, "coupons": list(self.coupons.values()), "loyalty": self.loyalty}


RULE_FIELDS = {
    "buy_x_get_y": ("sku", "buy", "get"),
    "bundle": ("skus", "price"),
    "percent": ("skus", "percent"),
    "basket": ("min_total", "off"),
}


def validate(raw: dict) -> None:
    """Raises ValueError with a readable message for a broken offers file."""
    seen = set()
    for rule in raw.get("promotions", []) + raw.get("coupons", []):
        kind = rule.get("type")
        if kind not in RULE_FIELDS:
            raise ValueError(f"Unknown offer type {kind!r} (use {', '.join(RULE_FIELDS)})")
        missing = [f for f in RULE_FIELDS[kind] if f not in rule]
        if missing:
            raise ValueError(f"Offer {rule.get('id') or rule.get('code')}: missing {', '.join(missing)}")
        if kind == "percent" and not 0 < rule["percent"] <= 90:
            raise ValueError("percent must be between 1 and 90")
        if kind == "buy_x_get_y" and (rule["buy"] < 1 or rule["get"] < 1):
            raise ValueError("buy and get must be at least 1")
        key = rule.get("id") or rule.get("code")
        if not key or key in seen:
            raise ValueError(f"Every offer needs a unique id (coupons: code): {key!r}")
        seen.add(key)
    for c in raw.get("coupons", []):
        if c.get("type") != "basket" and c.get("type") != "percent":
            raise ValueError("Coupons can be basket or percent offers")


def _lines_for(lines: list[dict], skus) -> list[dict]:
    skus = {skus} if isinstance(skus, str) else set(skus)
    return [ln for ln in lines if ln["sku"] in skus and not ln["used"]]


def _apply_line_rule(rule: dict, lines: list[dict]) -> int:
    """Applies one line offer to unused lines; marks the lines it used. Returns the discount."""
    kind = rule["type"]
    if kind == "buy_x_get_y":
        units = sorted(_lines_for(lines, rule["sku"]), key=lambda ln: ln["price"])
        group = rule["buy"] + rule["get"]
        sets = len(units) // group
        free = units[:sets * rule["get"]]
        for ln in units[:sets * group]:
            ln["used"] = True
        for ln in free:
            ln["discount"] += ln["price"]
        return sum(ln["price"] for ln in free)
    if kind == "bundle":
        pools = {sku: sorted(_lines_for(lines, sku), key=lambda ln: -ln["price"]) for sku in rule["skus"]}
        sets = min((len(p) for p in pools.values()), default=0)
        total = 0
        for i in range(sets):
            members = [pools[sku][i] for sku in rule["skus"]]
            value = sum(ln["price"] for ln in members)
            off = max(0, value - rule["price"])
            _spread(members, off)
            for ln in members:
                ln["used"] = True
            total += off
        return total
    if kind == "percent":
        total = 0
        for ln in _lines_for(lines, rule["skus"]):
            off = round(ln["price"] * rule["percent"] / 100)
            ln["discount"] += off
            ln["used"] = True
            total += off
        return total
    return 0


def _spread(lines: list[dict], amount: int) -> None:
    """Share `amount` over lines in proportion to what each still costs (largest remainder rounding)."""
    base = [max(0, ln["price"] - ln["discount"]) for ln in lines]
    whole = sum(base)
    if amount <= 0 or whole <= 0:
        return
    amount = min(amount, whole)
    shares = [amount * b // whole for b in base]
    rest = amount - sum(shares)
    order = sorted(range(len(lines)), key=lambda i: -(amount * base[i] % whole))
    for i in order[:rest]:
        shares[i] += 1
    for ln, s in zip(lines, shares):
        ln["discount"] += s


def price_basket(items: list[dict], products: dict, offers: Offers, coupon: str | None = None,
                 points: int = 0) -> dict:
    """items: [{line_id, sku, price}] (price in paise, GST included). products: sku -> Product."""
    lines = [{**it, "discount": 0, "used": False} for it in items]
    subtotal = sum(ln["price"] for ln in lines)
    applied: list[dict] = []
    for rule in offers.promotions:
        if rule["type"] == "basket":
            continue
        off = _apply_line_rule(rule, lines)
        if off:
            applied.append({"id": rule["id"], "label": rule.get("label", rule["id"]), "amount": off})

    def remaining() -> int:
        return sum(ln["price"] - ln["discount"] for ln in lines)

    for rule in [r for r in offers.promotions if r["type"] == "basket"]:
        if remaining() >= rule["min_total"]:
            off = min(rule["off"], remaining())
            _spread(lines, off)
            applied.append({"id": rule["id"], "label": rule.get("label", rule["id"]), "amount": off})

    coupon_rule = offers.coupons.get((coupon or "").upper()) if coupon else None
    coupon_result = None
    if coupon_rule:
        if coupon_rule["type"] == "basket":
            ok = remaining() >= coupon_rule["min_total"]
            off = min(coupon_rule["off"], remaining()) if ok else 0
            if ok:
                _spread(lines, off)
        else:  # percent coupon, on the listed SKUs (or everything with "*")
            targets = lines if coupon_rule["skus"] == "*" else [ln for ln in lines if ln["sku"] in coupon_rule["skus"]]
            off = 0
            for ln in targets:
                cut = round((ln["price"] - ln["discount"]) * coupon_rule["percent"] / 100)
                ln["discount"] += cut
                off += cut
        coupon_result = {"code": coupon_rule["code"], "label": coupon_rule.get("label", coupon_rule["code"]),
                         "amount": off, "applied": off > 0,
                         "reason": None if off else f"Spend at least Rs {coupon_rule.get('min_total', 0) / 100:.0f}"}
        if off:
            applied.append({"id": f"coupon:{coupon_rule['code']}", "label": coupon_result["label"], "amount": off})

    loyalty = offers.loyalty
    points_value = 0
    if points:
        points_value = min(points * loyalty["point_value_paise"], remaining())
        points = points_value // loyalty["point_value_paise"]
        points_value = points * loyalty["point_value_paise"]
        _spread(lines, points_value)
        if points_value:
            applied.append({"id": "loyalty", "label": f"{points} loyalty points", "amount": points_value})

    total = remaining()
    gst = gst_breakup(lines, products)
    for ln in lines:
        ln.pop("used", None)
        ln["net"] = ln["price"] - ln["discount"]
    return {
        "subtotal": subtotal,
        "discounts": applied,
        "discount_total": subtotal - total,
        "total": total,
        "coupon": coupon_result,
        "points_redeemed": points,
        "points_earned": total // 10000 * loyalty["earn_per_100_rupees"],
        "lines": lines,
        "gst": gst,
    }


def gst_breakup(lines: list[dict], products: dict) -> dict:
    """Tax inside each line's net price, grouped by rate. Amounts in paise."""
    groups: dict[str, dict] = {}
    outside = 0
    for ln in lines:
        product = products.get(ln["sku"])
        rate = getattr(product, "gst_rate", None)
        net = ln["price"] - ln["discount"]
        if rate is None:
            outside += net
            ln["taxable"], ln["tax"], ln["gst_rate"] = net, 0, None
            continue
        taxable = round(net * 100 / (100 + rate))
        ln["taxable"], ln["tax"], ln["gst_rate"] = taxable, net - taxable, rate
        g = groups.setdefault(str(rate), {"rate": rate, "taxable": 0, "tax": 0})
        g["taxable"] += taxable
        g["tax"] += net - taxable
    rows = []
    for g in sorted(groups.values(), key=lambda g: g["rate"]):
        cgst = g["tax"] // 2
        rows.append({**g, "cgst": cgst, "sgst": g["tax"] - cgst})
    return {"rates": rows, "taxable": sum(r["taxable"] for r in rows), "tax": sum(r["tax"] for r in rows),
            "outside_gst": outside}
