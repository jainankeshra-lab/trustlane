"""Payment providers.

* SandboxPayments (default): charges instantly with no gateway. Stands in for UPI Reserve Pay
  (funds blocked at entry, debited at exit), which is what true zero-touch exit billing needs.
* RazorpayPayments: a real gateway in TEST MODE ONLY. At the exit the server creates a Razorpay
  order; the shopper approves it in Razorpay Checkout on the phone (test UPI ID success@razorpay);
  the server verifies Razorpay's signature, confirms the payment over the API and captures it.

* CashfreeReservePay: UPI Reserve Pay in Cashfree's SANDBOX. Funds are blocked once per shopper and
  each trip is debited at the gate with no shopper action: the production zero-touch option.

UPI Autopay is not used for exit billing: NPCI requires a pre-debit notification 24 hours before
each debit, so it cannot charge the moment a shopper leaves.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import secrets

import httpx


class PaymentError(Exception):
    pass


class SandboxPayments:
    name = "sandbox"
    mode = "test"
    flow = "auto"  # charged by the server with no shopper action

    def __init__(self, fail_next: int = 0):
        self.fail_next = fail_next  # set >0 to simulate payment failures in a demo

    def charge(self, session_id: str, amount_paise: int) -> dict:
        if self.fail_next > 0:
            self.fail_next -= 1
            return {"status": "failed", "amount": amount_paise, "method": "auto-charge", "mode": self.mode,
                    "provider": self.name, "reason": "simulated decline"}
        return {"payment_id": f"pay_test_{secrets.token_hex(6)}", "status": "captured", "amount": amount_paise,
                "method": "auto-charge", "mode": self.mode, "provider": self.name}

    def refund(self, payment_id: str, amount_paise: int, receipt: dict | None = None) -> dict:
        return {"refund_id": f"rfnd_test_{secrets.token_hex(6)}", "payment_id": payment_id,
                "amount": amount_paise, "status": "processed", "provider": self.name}

    def public_config(self) -> dict:
        return {"provider": self.name, "flow": self.flow, "mode": self.mode}


class RazorpayPayments:
    name = "razorpay"
    mode = "test"
    flow = "checkout"  # the shopper approves on the phone
    API = "https://api.razorpay.com/v1"

    def __init__(self, key_id: str, key_secret: str, webhook_secret: str | None = None,
                 client: httpx.Client | None = None, store_name: str = "TrustLane store"):
        if not key_id.startswith("rzp_test_"):
            raise ValueError("Only Razorpay TEST keys (rzp_test_...) are allowed in this prototype")
        self.key_id = key_id
        self._secret = key_secret.encode()
        self._webhook_secret = webhook_secret.encode() if webhook_secret else None
        self.store_name = store_name
        self.client = client or httpx.Client(base_url=self.API, auth=(key_id, key_secret), timeout=10)

    def _call(self, method: str, path: str, **kwargs) -> dict:
        try:
            resp = self.client.request(method, path, **kwargs)
        except httpx.HTTPError as err:
            raise PaymentError(f"Could not reach Razorpay: {err}") from err
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("error", {}).get("description", resp.text)
            except ValueError:
                detail = resp.text
            raise PaymentError(f"Razorpay {method} {path} failed ({resp.status_code}): {detail}")
        return resp.json()

    def create_order(self, session_id: str, amount_paise: int) -> dict:
        return self._call("POST", "/orders", json={
            "amount": amount_paise, "currency": "INR", "receipt": session_id,
            "notes": {"session_id": session_id, "source": "trustlane-exit-gate"},
        })

    def checkout_options(self, order: dict) -> dict:
        """What the phone passes to Razorpay Checkout. Contains no secret."""
        return {"key": self.key_id, "order_id": order["id"], "amount": order["amount"],
                "currency": order.get("currency", "INR"), "name": self.store_name}

    def signature_ok(self, order_id: str, payment_id: str, signature: str) -> bool:
        expected = hmac.new(self._secret, f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature or "")

    def confirm(self, order_id: str, payment_id: str, signature: str) -> dict:
        """Check the Checkout signature, then ask Razorpay itself, then capture if needed."""
        if not self.signature_ok(order_id, payment_id, signature):
            raise PaymentError("Payment signature does not match this order")
        payment = self._call("GET", f"/payments/{payment_id}")
        if payment.get("order_id") != order_id:
            raise PaymentError("Payment belongs to a different order")
        if payment.get("status") == "authorized":
            payment = self._call("POST", f"/payments/{payment_id}/capture",
                                 json={"amount": payment["amount"], "currency": payment.get("currency", "INR")})
        if payment.get("status") != "captured":
            raise PaymentError(f"Payment status is {payment.get('status')}, not captured")
        return self.receipt(payment)

    def receipt(self, payment: dict) -> dict:
        return {"payment_id": payment["id"], "status": "captured", "amount": payment["amount"],
                "method": payment.get("method") or "upi", "vpa": payment.get("vpa"), "order_id": payment.get("order_id"),
                "mode": self.mode, "provider": self.name}

    def refund(self, payment_id: str, amount_paise: int, receipt: dict | None = None) -> dict:
        r = self._call("POST", f"/payments/{payment_id}/refund", json={"amount": amount_paise})
        return {"refund_id": r["id"], "payment_id": payment_id, "amount": r["amount"],
                "status": r.get("status", "processed"), "provider": self.name}

    def webhook_ok(self, raw_body: bytes, signature: str) -> bool:
        if not self._webhook_secret:
            return False
        expected = hmac.new(self._webhook_secret, raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature or "")

    def public_config(self) -> dict:
        return {"provider": self.name, "flow": self.flow, "mode": self.mode, "key_id": self.key_id}


class CashfreeReservePay:
    """UPI Reserve Pay (single block, multiple debits) through Cashfree, SANDBOX ONLY.

    Once per shopper: create an SBMD subscription and raise an authorisation the shopper approves in
    their UPI app; the funds stay blocked for up to 90 days. At each exit: raise a CHARGE against the
    block. No PIN, no tap, no 24-hour notice, so the gate can open the moment the decision is green.
    API version 2025-01-01, as used in Cashfree's Reserve Pay guide."""
    name = "cashfree_reserve_pay"
    mode = "test"
    flow = "mandate"
    API = "https://sandbox.cashfree.com/pg"
    API_VERSION = "2025-01-01"

    def __init__(self, client_id: str, client_secret: str, max_amount_paise: int = 500_000,
                 client: httpx.Client | None = None, return_url: str = "https://trustlane.invalid/mandate-done"):
        if not client_id or not client_secret:
            raise ValueError("Set CASHFREE_CLIENT_ID and CASHFREE_CLIENT_SECRET (sandbox) in trustlane/.env")
        self._secret = client_secret
        self.max_amount_paise = max_amount_paise
        self.return_url = return_url
        self.client = client or httpx.Client(base_url=self.API, timeout=15, headers={
            "x-client-id": client_id, "x-client-secret": client_secret, "x-api-version": self.API_VERSION})

    def _call(self, method: str, path: str, **kwargs) -> dict:
        try:
            resp = self.client.request(method, path, **kwargs)
        except httpx.HTTPError as err:
            raise PaymentError(f"Could not reach Cashfree: {err}") from err
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("message", resp.text)
            except ValueError:
                detail = resp.text
            raise PaymentError(f"Cashfree {method} {path} failed ({resp.status_code}): {detail}")
        return resp.json() if resp.content else {}

    def create_mandate(self, shopper_id: str, customer: dict, upi_id: str | None, now: float) -> dict:
        import datetime as dt
        sub_id = f"tl_{shopper_id}_{int(now)}"
        expiry = dt.datetime.fromtimestamp(now + 89 * 86400, dt.timezone(dt.timedelta(hours=5, minutes=30)))
        sub = self._call("POST", "/subscriptions", json={
            "subscription_id": sub_id,
            "customer_details": {"customer_name": customer.get("name") or "TrustLane shopper",
                                 "customer_email": customer.get("email") or "shopper@example.com",
                                 "customer_phone": customer.get("phone") or "9999999999"},
            "plan_details": {"plan_name": "TrustLane Reserve Pay", "plan_type": "ON_DEMAND", "plan_currency": "INR",
                             "plan_max_amount": self.max_amount_paise / 100},
            "authorization_details": {"authorization_amount": 0, "authorization_amount_refund": True,
                                      "payment_methods": ["upi"]},
            "subscription_meta": {"return_url": self.return_url, "category": "SBMD"},
            "subscription_expiry_time": expiry.isoformat(timespec="seconds"),
            "subscription_note": "Funds blocked for TrustLane scan-and-go; each trip is debited at the exit gate",
        })
        upi = {"channel": "collect", "upi_id": upi_id} if upi_id else {"channel": "link"}
        auth = self._call("POST", "/subscriptions/pay", json={
            "subscription_id": sub_id, "payment_id": f"{sub_id}_auth", "payment_type": "AUTH",
            "payment_remarks": "Approve TrustLane Reserve Pay", "payment_method": {"upi": upi}})
        return {"subscription_id": sub_id, "status": sub.get("subscription_status", "INITIALIZED"), "auth": auth}

    def charge(self, subscription_id: str, trip_id: str, amount_paise: int, now: float) -> dict:
        import datetime as dt
        when = dt.datetime.fromtimestamp(now, dt.timezone(dt.timedelta(hours=5, minutes=30)))
        r = self._call("POST", "/subscriptions/pay", json={
            "subscription_id": subscription_id, "payment_id": f"{trip_id}_{int(now)}", "payment_type": "CHARGE",
            "payment_amount": round(amount_paise / 100, 2), "payment_schedule_date": when.isoformat(timespec="seconds"),
            "payment_remarks": f"TrustLane trip {trip_id}"})
        return {"payment_id": r.get("payment_id", f"{trip_id}_{int(now)}"), "cf_payment_id": r.get("cf_payment_id"),
                "status": "captured", "amount": amount_paise, "method": "UPI Reserve Pay", "mode": self.mode,
                "provider": self.name, "gateway_status": r.get("payment_status", "PENDING"),
                "note": "Debited from your reserved UPI funds"}

    def refund(self, payment_id: str, amount_paise: int, receipt: dict | None = None) -> dict:
        """POST /subscriptions/{id}/refunds (API 2025-01-01). UPI refunds are STANDARD speed, so the
        money reaches the shopper's bank within a few working days; status starts as PENDING."""
        sub_id = (receipt or {}).get("subscription_id")
        if not sub_id:
            raise PaymentError("This receipt has no Reserve Pay subscription id")
        refund_id = f"{payment_id}_r{len((receipt or {}).get('refunds', [])) + 1}"
        body = {"subscription_id": sub_id, "payment_id": payment_id, "refund_id": refund_id,
                "refund_amount": round(amount_paise / 100, 2), "refund_note": "TrustLane refund"}
        if (receipt or {}).get("cf_payment_id"):
            with contextlib.suppress(ValueError, TypeError):
                body["cf_payment_id"] = int(receipt["cf_payment_id"])
        r = self._call("POST", f"/subscriptions/{sub_id}/refunds", json=body, headers={"x-idempotency-key": refund_id})
        status = r.get("refund_status", "PENDING")
        if status in ("FAILED", "CANCEL"):
            raise PaymentError(f"Cashfree refused the refund ({status})")
        return {"refund_id": refund_id, "cf_refund_id": r.get("cf_refund_id"), "payment_id": payment_id,
                "amount": amount_paise, "status": "processed" if status == "SUCCESS" else "pending",
                "gateway_status": status, "provider": self.name}

    def cancel_mandate(self, subscription_id: str) -> dict:
        """Releases the blocked funds (shopper deleted their account, or asked to unlink)."""
        return self._call("POST", f"/subscriptions/{subscription_id}/manage",
                          json={"subscription_id": subscription_id, "action": "CANCEL"})

    def webhook_ok(self, raw_body: bytes, signature: str | None, timestamp: str | None, now: float) -> bool:
        """Cashfree: Base64(HMAC-SHA256(x-webhook-timestamp + raw body, client secret)), within 5 minutes."""
        if not signature or not timestamp:
            return False
        try:
            ts = float(timestamp)
        except ValueError:
            return False
        if abs(now - (ts / 1000 if ts > 1e12 else ts)) > 300:
            return False
        digest = hmac.new(self._secret.encode(), timestamp.encode() + raw_body, hashlib.sha256).digest()
        return hmac.compare_digest(base64.b64encode(digest).decode(), signature)

    def public_config(self) -> dict:
        return {"provider": self.name, "flow": self.flow, "mode": self.mode, "max_amount": self.max_amount_paise}


def payments_from_settings(settings):
    """Built-in sandbox unless gateway credentials are configured. TRUSTLANE_PAYMENTS forces a choice:
    sandbox | razorpay | cashfree. With "auto", Cashfree Reserve Pay (zero-touch) wins over Razorpay."""
    choice = settings.payments_provider
    if choice == "cashfree" or (choice == "auto" and settings.cashfree_client_id):
        return CashfreeReservePay(settings.cashfree_client_id, settings.cashfree_client_secret,
                                  int(settings.reserve_pay_max_paise))
    if choice == "razorpay" or (choice == "auto" and settings.razorpay_key_id):
        if not (settings.razorpay_key_id and settings.razorpay_key_secret):
            raise ValueError("Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET (test mode) in trustlane/.env")
        return RazorpayPayments(settings.razorpay_key_id, settings.razorpay_key_secret, settings.razorpay_webhook_secret)
    return SandboxPayments()
