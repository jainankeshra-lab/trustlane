"""Offers, coupons, loyalty points, GST invoices and credit notes, shopper accounts, DPDP rights,
catalog and offer editing, and push tickets."""
import shutil
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from server.catalog import Catalog
from server.config import ROOT
from server.invoice import gstin_valid, rupees_in_words
from server.main import create_app
from server.pricing import Offers, price_basket, validate
from server.push import PushSender
from tests.helpers import Phone, staff
from tests.test_api import make_settings

BC = {"gum": "8901000000059", "milk": "8901000000066", "bread": "8901000000073", "chocolate": "8901000000035",
      "beer": "8901000000080", "razor": "8901000000011"}
PRODUCTS = {p.sku: p for p in Catalog.load(ROOT / "data" / "catalog.json").all()}


def basket(*skus):
    return [{"line_id": f"L{i}", "sku": s, "price": PRODUCTS[s].price} for i, s in enumerate(skus)]


def offers(**kw):
    return Offers(kw.get("promotions", []), {c["code"]: c for c in kw.get("coupons", [])},
                  kw.get("loyalty", {"earn_per_100_rupees": 1, "point_value_paise": 100, "min_redeem": 50}))


# ---------------------------------------------------------------- pricing rules
def test_buy_x_get_y_frees_the_right_number_of_units():
    o = offers(promotions=[{"id": "g", "type": "buy_x_get_y", "sku": "gum", "buy": 2, "get": 1}])
    assert price_basket(basket("gum", "gum"), PRODUCTS, o)["discount_total"] == 0
    assert price_basket(basket("gum", "gum", "gum"), PRODUCTS, o)["discount_total"] == 3000
    assert price_basket(basket(*["gum"] * 7), PRODUCTS, o)["discount_total"] == 6000


def test_bundle_percent_basket_coupon_and_points_add_up():
    o = offers(promotions=[{"id": "b", "type": "bundle", "skus": ["milk", "bread"], "price": 11000},
                           {"id": "p", "type": "percent", "skus": ["chocolate"], "percent": 10},
                           {"id": "k", "type": "basket", "min_total": 20000, "off": 1000}],
               coupons=[{"code": "X", "type": "basket", "min_total": 0, "off": 500}])
    r = price_basket(basket("milk", "bread", "chocolate", "razor"), PRODUCTS, o, coupon="x", points=60)
    assert [d["id"] for d in r["discounts"]] == ["b", "p", "k", "coupon:X", "loyalty"]
    assert r["discount_total"] == 1300 + 1900 + 1000 + 500 + 6000
    assert sum(ln["net"] for ln in r["lines"]) == r["total"] == r["subtotal"] - r["discount_total"]
    assert r["points_earned"] == r["total"] // 10000


def test_coupon_below_minimum_says_why():
    o = offers(coupons=[{"code": "BIG", "type": "basket", "min_total": 100000, "off": 5000}])
    r = price_basket(basket("gum"), PRODUCTS, o, coupon="BIG")
    assert r["coupon"]["applied"] is False and "Rs 1000" in r["coupon"]["reason"] and r["discount_total"] == 0


def test_gst_is_charged_on_the_discounted_price_and_beer_is_outside_gst():
    o = offers(promotions=[{"id": "p", "type": "percent", "skus": ["razor"], "percent": 50}])
    r = price_basket(basket("razor", "beer", "milk"), PRODUCTS, o)
    rates = {row["rate"]: row for row in r["gst"]["rates"]}
    assert rates[18]["taxable"] + rates[18]["tax"] == 14950  # Rs 299 at 50% off
    assert rates[18]["tax"] == 14950 - round(14950 * 100 / 118)
    assert rates[18]["cgst"] + rates[18]["sgst"] == rates[18]["tax"]
    assert r["gst"]["outside_gst"] == PRODUCTS["beer"].price and rates[0]["tax"] == 0


def test_offer_files_are_validated():
    with pytest.raises(ValueError):
        validate({"promotions": [{"id": "x", "type": "magic"}]})
    with pytest.raises(ValueError):
        validate({"promotions": [{"id": "x", "type": "percent", "skus": ["gum"], "percent": 150}]})
    with pytest.raises(ValueError):
        validate({"promotions": [{"id": "x", "type": "basket", "min_total": 1, "off": 1},
                                 {"id": "x", "type": "basket", "min_total": 1, "off": 1}]})
    validate({"promotions": [{"id": "x", "type": "bundle", "skus": ["milk", "bread"], "price": 100}]})


def test_gstin_and_amount_in_words():
    assert gstin_valid("27AAPFU0939F1ZV") and not gstin_valid("27AAPFU0939F1ZX") and not gstin_valid("bad")
    assert rupees_in_words(150050) == "Rupees one thousand five hundred and fifty paise only"


# ---------------------------------------------------------------- through the API
@pytest.fixture
def shop(tmp_path):
    shutil.copy(ROOT / "data" / "promotions.json", tmp_path / "offers.json")
    shutil.copy(ROOT / "data" / "catalog.json", tmp_path / "catalog.json")
    settings = make_settings(tmp_path, offers_path=tmp_path / "offers.json", catalog_path=tmp_path / "catalog.json")
    with TestClient(create_app(settings)) as c:
        yield c


def scan_all(p, *skus):
    for s in skus:
        assert p.scan(BC[s]).status_code == 200


def test_cart_shows_offers_and_the_exit_issues_a_gst_invoice(shop):
    p = Phone(shop)
    scan_all(p, "gum", "gum", "gum", "chocolate")
    cart = p.cart()
    assert cart["subtotal"] == 3 * 3000 + 19000 and cart["total"] == cart["subtotal"] - 3000 - 1900
    assert {d["id"] for d in cart["discounts"]} == {"gum-3-for-2", "choc-10"}
    p.exit()
    receipt = p.cart()["receipt"]
    inv = receipt["invoice"]
    assert inv["number"].startswith("TL/20") and inv["number"].endswith("/000001")
    html = shop.get(inv["url"])
    assert html.status_code == 200 and "27AAPFU0939F1ZV" in html.text and "Tax Invoice" in html.text
    assert shop.get(inv["url"].split("?")[0] + "?t=forged").status_code == 403
    doc = p.get("/invoice").json()
    assert doc["data"]["total"] == cart["total"] and doc["kind"] == "invoice"
    assert receipt["points"]["earned"] == cart["total"] // 10000


def test_coupon_once_per_shopper_and_points_round_trip(shop):
    p = Phone(shop)
    scan_all(p, "razor")
    assert p.post("/coupon", {"code": "nope"}).status_code == 404
    cart = p.post("/coupon", {"code": "welcome50"}).json()
    assert cart["coupon"]["applied"] and cart["total"] == 29900 - 5000
    p.exit()
    earned = p.cart()["receipt"]["points"]["balance"]
    assert earned == (29900 - 5000) // 10000

    again = Phone(shop, shopper=p.shopper)
    scan_all(again, "razor")
    assert again.post("/coupon", {"code": "WELCOME50"}).status_code == 409  # already used
    assert again.post("/points", {"points": earned + 1}).status_code == 400


def test_refund_returns_what_was_paid_and_issues_a_credit_note(shop):
    p = Phone(shop)
    scan_all(p, "chocolate")
    p.exit()
    line = p.cart()["lines"][0]["line_id"]
    r = shop.post(f"/api/ops/trips/{p.sid}/refund", json={"line_ids": [line], "reason": "damaged"}, headers=staff(shop.app))
    assert r.status_code == 200, r.text
    refund = r.json()["receipt"]["refunds"][-1]
    assert refund["amount"] == 19000 - 1900  # the shopper paid the offer price
    cn = refund["credit_note"]
    assert cn["number"].startswith("TL-CN/") and shop.get(cn["url"]).status_code == 200


# ---------------------------------------------------------------- accounts, devices, e-mail
def otp_code(client, monkeypatch):
    codes = []
    monkeypatch.setattr(client.app.state.accounts.sms, "send", lambda to, text: codes.append(text.split()[0]))
    return codes


def test_phone_sign_in_links_devices_and_emails_invoices(shop, monkeypatch):
    codes = otp_code(shop, monkeypatch)
    sent = []
    mailer = shop.app.state.accounts.mailer
    monkeypatch.setattr(mailer, "_send_fn", sent.append)
    first = Phone(shop)
    assert shop.post("/api/shoppers/otp/start", json={"phone": "98765 43210"}).json()["sent_to"].endswith("3210")
    assert shop.post("/api/shoppers/otp/verify", json={"phone": "9876543210", "code": "000000"}).status_code == 400
    acct = shop.post("/api/shoppers/otp/verify", json={"phone": "+919876543210", "code": codes[-1], **first.shopper}).json()
    assert acct["new"] and acct["shopper_id"] == first.shopper["shopper_id"]
    creds = {"shopper_id": acct["shopper_id"], "shopper_secret": acct["shopper_secret"]}
    assert shop.post("/api/shoppers/me/profile", json={**creds, "email": "not-an-email"}).status_code == 400
    shop.post("/api/shoppers/me/profile", json={**creds, "email": "asha@example.com", "name": "Asha"})

    shop.post("/api/shoppers/otp/start", json={"phone": "9876543210"})  # same person, second phone
    second = shop.post("/api/shoppers/otp/verify", json={"phone": "9876543210", "code": codes[-1]}).json()
    assert not second["new"] and second["shopper_id"] == creds["shopper_id"]
    other = {"shopper_id": second["shopper_id"], "shopper_secret": second["shopper_secret"]}
    p = Phone(shop, shopper=other)
    scan_all(p, "milk")
    p.exit()
    assert any(r["number"] == p.cart()["receipt"]["invoice"]["number"]
               for r in shop.post("/api/shoppers/me", json=creds).json()["receipts"])
    for _ in range(50):
        if sent:
            break
        time.sleep(0.05)
    assert sent and sent[0]["To"] == "asha@example.com" and "invoice" in sent[0]["Subject"].lower()


def test_otp_attempts_are_limited(shop, monkeypatch):
    codes = otp_code(shop, monkeypatch)
    shop.post("/api/shoppers/otp/start", json={"phone": "9123456789"})
    for _ in range(5):
        shop.post("/api/shoppers/otp/verify", json={"phone": "9123456789", "code": "111111"})
    r = shop.post("/api/shoppers/otp/verify", json={"phone": "9123456789", "code": codes[-1]})
    assert r.status_code == 400 and "expired" in r.json()["detail"]


# ---------------------------------------------------------------- DPDP: access and erasure
def test_export_and_erasure(shop):
    p = Phone(shop)
    creds = p.shopper
    scan_all(p, "bread")
    assert shop.post("/api/shoppers/me/delete", json={**creds, "confirm": True}).status_code == 409  # trip open
    p.exit()
    number = p.cart()["receipt"]["invoice"]["number"]
    export = shop.post("/api/shoppers/me/export", json=creds).json()
    assert export["invoices"][0]["number"] == number and export["trips"][0]["session_id"] == p.sid
    assert shop.post("/api/shoppers/me/delete", json=creds).status_code == 400  # needs confirm
    gone = shop.post("/api/shoppers/me/delete", json={**creds, "confirm": True}).json()
    assert gone["erased"] and gone["invoices_unlinked"] == 1
    assert shop.post("/api/shoppers/me", json=creds).status_code == 401
    kept = shop.app.state.engine.ledger.get_invoice(number)
    assert kept and kept["shopper_id"].startswith("erased-")  # GST records stay, without the person
    assert shop.app.state.engine.ledger.verify_chain()


# ---------------------------------------------------------------- catalog and offers editing
def test_supervisors_edit_the_catalog_with_validation(shop):
    from tests.test_staff_accounts import bearer
    pin = staff(shop.app)
    for u, role in (("asha", "associate"), ("ravi", "supervisor")):
        shop.post("/api/staff/users", headers=pin, json={"username": u, "role": role, "password": f"{u}-password"})
    asha, ravi = bearer(shop, "asha", "asha-password"), bearer(shop, "ravi", "ravi-password")
    new = {"sku": "soap", "barcode": "8901000000097", "name": "Soap 100 g", "price": 4500, "unit_weight_g": 110,
           "aisle": "4", "hsn": "3401", "gst_rate": 18}
    assert shop.post("/api/catalog/products", json=new, headers=asha).status_code == 403
    assert shop.post("/api/catalog/products", json=new, headers=ravi).status_code == 200
    dup = shop.post("/api/catalog/products", json={**new, "sku": "soap2"}, headers=ravi)
    assert dup.status_code == 400 and "already belongs" in dup.json()["detail"]
    lane = shop.post("/api/catalog/products", json={**new, "sku": "soap3", "barcode": "8901000000098",
                                                    "shelf_id": "S7-A", "lane": 0}, headers=ravi)
    assert lane.status_code == 400 and "lane" in lane.json()["detail"]
    assert shop.post("/api/catalog/products", json={**new, "gst_rate": 7}, headers=ravi).status_code == 400
    assert any(p["sku"] == "soap" for p in shop.get("/api/catalog").json())
    saved = Catalog.load(ROOT / "data" / "catalog.json")
    assert saved.get("soap") is None  # the real catalog file is untouched; the test's copy was edited
    assert shop.delete("/api/catalog/products/soap", headers=ravi).status_code == 200


def test_offers_can_be_edited_and_bad_ones_are_refused(shop):
    pin = staff(shop.app)
    current = shop.get("/api/ops/offers", headers=pin).json()
    bad = shop.put("/api/ops/offers", json={**current, "promotions": [{"id": "x", "type": "magic"}]}, headers=pin)
    assert bad.status_code == 400
    current["promotions"] = [{"id": "milk-half", "type": "percent", "skus": ["milk"], "percent": 50, "label": "Half-price milk"}]
    assert shop.put("/api/ops/offers", json=current, headers=pin).status_code == 200
    p = Phone(shop)
    scan_all(p, "milk")
    assert p.cart()["total"] == 6800 // 2


# ---------------------------------------------------------------- push tickets
@pytest.mark.anyio
async def test_dead_push_tokens_are_dropped():
    dead = []

    def expo(request):
        return httpx.Response(200, json={"data": [{"status": "ok", "id": "t1"},
                                                  {"status": "error", "details": {"error": "DeviceNotRegistered"}}]})
    sender = PushSender(httpx.AsyncClient(transport=httpx.MockTransport(expo)), on_dead_token=dead.append)
    tickets = await sender.send([{"to": "ExponentPushToken[a]"}, {"to": "ExponentPushToken[b]"}])
    assert len(tickets) == 2 and dead == ["ExponentPushToken[b]"]


def test_push_test_endpoint(shop):
    p = Phone(shop)
    assert p.post("/push-test").status_code == 409
