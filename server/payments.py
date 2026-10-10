"""Payment providers.

* SandboxPayments (default): charges instantly with no gateway. Stands in for UPI Reserve Pay
  (funds blocked at entry, debited at exit), which is what true zero-touch exit billing needs.
* RazorpayPayments: a real gateway in TEST MODE ONLY. At the exit the server creates a Razorpay
  order; the shopper approves it in Razorpay Checkout on the phone (test UPI ID success@razorpay);
  the server verifies Razorpay's signature, confirms the payment over the API and captures it.

UPI Autopay is not used for exit billing: NPCI requires a pre-debit notification 24 hours before
each debit, so it cannot charge the moment a shopper leaves.
"""
from __future__ import annotations

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

    def refund(self, payment_id: str, amount_paise: int) -> dict:
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

    def refund(self, payment_id: str, amount_paise: int) -> dict:
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


def payments_from_settings(settings):
    """sandbox unless Razorpay test keys are configured (TRUSTLANE_PAYMENTS=sandbox forces sandbox)."""
    choice = settings.payments_provider
    if choice == "razorpay" or (choice == "auto" and settings.razorpay_key_id):
        if not (settings.razorpay_key_id and settings.razorpay_key_secret):
            raise ValueError("Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET (test mode) in trustlane/.env")
        return RazorpayPayments(settings.razorpay_key_id, settings.razorpay_key_secret, settings.razorpay_webhook_secret)
    return SandboxPayments()
