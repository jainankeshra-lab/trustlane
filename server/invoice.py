"""GST tax invoices and credit notes.

Every paid trip gets a tax invoice numbered in sequence within the Indian financial year (April to
March), e.g. TL/2026-27/000042; every refund gets a credit note (TL-CN/2026-27/000003) that reverses
the tax on what was refunded. The store's legal details come from data/store.json. Invoices are kept
in the ledger database for the 72 months GST law asks for, and are reachable by the shopper through a
signed link (no sign-in needed to open your own receipt).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import html
import json
import re
import secrets
from pathlib import Path

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
GSTIN_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

DEFAULT_STORE = {
    "legal_name": "TrustLane Demo Retail Pvt Ltd",
    "trade_name": "TrustLane Demo Store",
    "gstin": "27AAPFU0939F1ZV",
    "address": "Shop 1, Demo Market, Pune 411001",
    "state": "Maharashtra",
    "state_code": "27",
    "invoice_prefix": "TL",
    "phone": "",
    "grievance_officer": {"name": "Grievance Officer", "email": "privacy@example.com"},
}


def gstin_checksum(first14: str) -> str:
    total = 0
    for i, ch in enumerate(first14):
        product = GSTIN_CHARS.index(ch) * (1 if i % 2 == 0 else 2)
        total += product // 36 + product % 36
    return GSTIN_CHARS[(36 - total % 36) % 36]


def gstin_valid(gstin: str) -> bool:
    gstin = (gstin or "").strip().upper()
    return bool(GSTIN_RE.match(gstin)) and gstin_checksum(gstin[:14]) == gstin[14]


def load_store(data_dir: Path) -> dict:
    path = Path(data_dir) / "store.json"
    store = {**DEFAULT_STORE, **(json.loads(path.read_text(encoding="utf-8")) if path.exists() else {})}
    if not gstin_valid(store["gstin"]):
        raise ValueError(f"store.json: GSTIN {store['gstin']!r} is not valid (format or check digit)")
    store["demo"] = not path.exists()
    return store


def financial_year(ts: float) -> str:
    d = dt.datetime.fromtimestamp(ts, IST)
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start}-{str(start + 1)[2:]}"


# ---------------------------------------------------------------- amounts in words (Indian system)
_ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
         "sixteen seventeen eighteen nineteen").split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()


def _below_1000(n: int) -> str:
    parts = []
    if n >= 100:
        parts.append(f"{_ONES[n // 100]} hundred")
        n %= 100
    if n >= 20:
        parts.append(_TENS[n // 10] + (f"-{_ONES[n % 10]}" if n % 10 else ""))
    elif n or not parts:
        parts.append(_ONES[n])
    return " ".join(parts)


def words(n: int) -> str:
    if n == 0:
        return "zero"
    parts = []
    for size, name in ((10 ** 7, "crore"), (10 ** 5, "lakh"), (1000, "thousand")):
        if n >= size:
            parts.append(f"{words(n // size) if size == 10 ** 7 else _below_1000(n // size)} {name}")
            n %= size
    if n:
        parts.append(_below_1000(n))
    return " ".join(parts)


def rupees_in_words(paise: int) -> str:
    r, p = divmod(paise, 100)
    text = f"Rupees {words(r)}"
    if p:
        text += f" and {words(p)} paise"
    return text[0].upper() + text[1:] + " only"


# ---------------------------------------------------------------- building documents
class Invoicer:
    def __init__(self, ledger, data_dir: Path, store: dict):
        self.ledger = ledger
        self.store = store
        key_path = Path(data_dir) / "invoice_key"
        if not key_path.exists():
            key_path.write_text(secrets.token_hex(32))
        self._key = bytes.fromhex(key_path.read_text().strip())

    def link_token(self, number: str) -> str:
        return hmac.new(self._key, number.encode(), hashlib.sha256).hexdigest()[:24]

    def url(self, number: str) -> str:
        return f"/invoice/{number.replace('/', '_')}?t={self.link_token(number)}"

    def check_link(self, number: str, token: str) -> bool:
        return hmac.compare_digest(self.link_token(number), token or "")

    def _lines(self, pricing: dict, lines_by_id: dict, products: dict) -> list[dict]:
        out = []
        for pl in pricing["lines"]:
            cart_line = lines_by_id.get(pl["line_id"])
            product = products.get(pl["sku"])
            out.append({
                "line_id": pl["line_id"], "sku": pl["sku"],
                "description": product.name if product else pl["sku"],
                "hsn": getattr(product, "hsn", None),
                "qty": f"{cart_line.grams:.0f} g" if cart_line and cart_line.grams else "1",
                "mrp": pl["price"], "discount": pl["discount"], "net": pl["net"],
                "gst_rate": pl["gst_rate"], "taxable": pl["taxable"], "tax": pl["tax"],
            })
        return out

    def issue(self, sess, pricing: dict, payment: dict, products: dict, now: float) -> dict:
        lines_by_id = {ln.line_id: ln for ln in sess.lines}
        doc = {
            "kind": "invoice", "title": "Tax Invoice",
            "date": dt.datetime.fromtimestamp(now, IST).strftime("%d-%m-%Y %H:%M"),
            "store": {k: v for k, v in self.store.items() if k != "grievance_officer"},
            "place_of_supply": f"{self.store['state']} ({self.store['state_code']})",
            "trip": sess.session_id,
            "lines": self._lines(pricing, lines_by_id, products),
            "subtotal": pricing["subtotal"], "discounts": pricing["discounts"],
            "total": pricing["total"], "gst": pricing["gst"],
            "in_words": rupees_in_words(pricing["total"]),
            "payment": {k: payment.get(k) for k in ("provider", "method", "payment_id", "mode")},
        }
        number = self.ledger.save_invoice("invoice", self.store["invoice_prefix"], financial_year(now),
                                          sess.session_id, sess.shopper_id, now, pricing["total"], doc)
        return {"number": number, "url": self.url(number), "total": pricing["total"]}

    def credit_note(self, sess, invoice_number: str, line_ids: list[str], reason: str, now: float) -> dict:
        invoice = self.ledger.get_invoice(invoice_number)
        if not invoice:
            raise ValueError(f"No invoice {invoice_number}")
        doc_lines = [ln for ln in invoice["data"]["lines"] if ln["line_id"] in line_ids]
        rates: dict[str, dict] = {}
        outside = 0
        for ln in doc_lines:
            if ln["gst_rate"] is None:
                outside += ln["net"]
                continue
            g = rates.setdefault(str(ln["gst_rate"]), {"rate": ln["gst_rate"], "taxable": 0, "tax": 0})
            g["taxable"] += ln["taxable"]
            g["tax"] += ln["tax"]
        rows = [{**g, "cgst": g["tax"] // 2, "sgst": g["tax"] - g["tax"] // 2} for g in rates.values()]
        total = sum(ln["net"] for ln in doc_lines)
        doc = {
            "kind": "credit_note", "title": "Credit Note",
            "date": dt.datetime.fromtimestamp(now, IST).strftime("%d-%m-%Y %H:%M"),
            "store": invoice["data"]["store"], "place_of_supply": invoice["data"]["place_of_supply"],
            "trip": sess.session_id, "against_invoice": invoice_number, "reason": reason,
            "lines": doc_lines, "subtotal": sum(ln["mrp"] for ln in doc_lines), "discounts": [],
            "total": total, "gst": {"rates": rows, "taxable": sum(r["taxable"] for r in rows),
                                    "tax": sum(r["tax"] for r in rows), "outside_gst": outside},
            "in_words": rupees_in_words(total),
        }
        number = self.ledger.save_invoice("credit_note", f"{self.store['invoice_prefix']}-CN", financial_year(now),
                                          sess.session_id, sess.shopper_id, now, total, doc)
        return {"number": number, "url": self.url(number), "total": total}


def money(p: int) -> str:
    return f"{p / 100:,.2f}"


def render_html(number: str, doc: dict) -> str:
    e = html.escape
    s = doc["store"]
    rows = "".join(
        f"<tr><td>{e(ln['description'])}</td><td>{e(ln['hsn'] or '')}</td><td class=n>{e(str(ln['qty']))}</td>"
        f"<td class=n>{money(ln['mrp'])}</td><td class=n>{money(ln['discount'])}</td>"
        f"<td class=n>{'outside GST' if ln['gst_rate'] is None else str(ln['gst_rate']) + '%'}</td>"
        f"<td class=n>{money(ln['taxable'])}</td><td class=n>{money(ln['net'])}</td></tr>" for ln in doc["lines"])
    gst_rows = "".join(
        f"<tr><td>{r['rate']}%</td><td class=n>{money(r['taxable'])}</td><td class=n>{money(r['cgst'])}</td>"
        f"<td class=n>{money(r['sgst'])}</td></tr>" for r in doc["gst"]["rates"])
    if doc["gst"]["outside_gst"]:
        gst_rows += f"<tr><td>Outside GST (state excise)</td><td class=n>{money(doc['gst']['outside_gst'])}</td><td></td><td></td></tr>"
    discounts = "".join(f"<tr><td>{e(d['label'])}</td><td class=n>-{money(d['amount'])}</td></tr>" for d in doc["discounts"])
    against = (f"<p>Against invoice <b>{e(doc['against_invoice'])}</b>. Reason: {e(doc.get('reason') or '')}</p>"
               if doc["kind"] == "credit_note" else "")
    pay = doc.get("payment") or {}
    demo = "<p class=demo>Demo store details: replace data/store.json with your registered GSTIN.</p>" if s.get("demo") else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{e(doc['title'])} {e(number)}</title>
<style>
body{{font:14px/1.45 system-ui,sans-serif;color:#10201B;max-width:820px;margin:0 auto;padding:20px 16px;background:#fff}}
h1{{font-size:20px;margin:0}} table{{width:100%;border-collapse:collapse;margin:10px 0}}
th,td{{border-bottom:1px solid #D3DCD6;padding:6px 4px;text-align:left;vertical-align:top}} .n{{text-align:right;font-variant-numeric:tabular-nums}}
.head{{display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap}} .muted{{color:#5E6B66}} .total{{font-size:18px;font-weight:700}}
.demo{{background:#FDF1DC;padding:6px 10px;border-radius:8px}} .wrap{{overflow-x:auto}}
@media print{{.demo{{display:none}}}}
</style></head><body>
{demo}
<div class="head"><div><h1>{e(s['legal_name'])}</h1><div class="muted">{e(s['trade_name'])}<br>{e(s['address'])}<br>GSTIN {e(s['gstin'])}</div></div>
<div><h1>{e(doc['title'])}</h1><div>No. <b>{e(number)}</b><br>Date {e(doc['date'])}<br>Place of supply {e(doc['place_of_supply'])}<br>Trip {e(doc['trip'])}</div></div></div>
{against}
<div class="wrap"><table><thead><tr><th>Item</th><th>HSN</th><th class=n>Qty</th><th class=n>MRP</th><th class=n>Discount</th><th class=n>GST</th><th class=n>Taxable</th><th class=n>Amount</th></tr></thead>
<tbody>{rows}</tbody></table></div>
{f'<table><tbody>{discounts}</tbody></table>' if discounts else ''}
<div class="wrap"><table><thead><tr><th>GST rate</th><th class=n>Taxable value</th><th class=n>CGST</th><th class=n>SGST</th></tr></thead><tbody>{gst_rows}</tbody></table></div>
<p class="total">Total {'refunded' if doc['kind'] == 'credit_note' else 'paid'}: Rs {money(doc['total'])}</p>
<p>{e(doc['in_words'])}</p>
<p class="muted">Prices include GST. {('Paid by ' + e(str(pay.get('method') or '')) + ' (' + e(str(pay.get('provider') or '')) + ', ' + e(str(pay.get('payment_id') or '')) + ')') if pay else ''}</p>
</body></html>"""
