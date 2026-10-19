"""Shopper accounts: sign in with a phone number (one-time code by SMS), use the account on more than
one phone, see past receipts, and get each invoice by e-mail. Plus the shopper's rights under India's
DPDP Act 2023: a copy of their data, and erasure.

Without Twilio settings the one-time code is written to the server log (development). Without SMTP
settings invoices aren't e-mailed; they are always in the app and at their signed link.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage

import httpx

log = logging.getLogger("trustlane.accounts")
PHONE_RE = re.compile(r"^\+?[1-9]\d{9,14}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
OTP_TTL_S = 300
OTP_MAX_ATTEMPTS = 5


class AccountError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def normalise_phone(phone: str) -> str:
    """Indian 10-digit numbers get +91; anything else must already be in +<country><number> form."""
    digits = re.sub(r"[\s\-()]", "", phone or "")
    if re.fullmatch(r"[6-9]\d{9}", digits):
        digits = "+91" + digits
    if not PHONE_RE.match(digits):
        raise AccountError(400, "Enter a mobile number, e.g. 98765 43210")
    return digits if digits.startswith("+") else "+" + digits


def _hash_code(phone: str, code: str) -> str:
    return hashlib.sha256(f"{phone}|{code}".encode()).hexdigest()


class SmsSender:
    def __init__(self, sid: str = "", token: str = "", sender: str = "", client: httpx.Client | None = None):
        self.sid, self.token, self.sender = sid, token, sender
        self.client = client or httpx.Client(timeout=10)

    @property
    def configured(self) -> bool:
        return bool(self.sid and self.token and self.sender)

    def send(self, to: str, text: str) -> None:
        if not self.configured:
            log.warning("SMS to %s (Twilio not configured, development only): %s", to, text)
            return
        r = self.client.post(f"https://api.twilio.com/2010-04-01/Accounts/{self.sid}/Messages.json",
                             data={"To": to, "From": self.sender, "Body": text}, auth=(self.sid, self.token))
        if r.status_code >= 400:
            raise AccountError(502, "Couldn't send the SMS code. Try again shortly.")


class Mailer:
    """Sends invoice e-mails in the background (SMTP over TLS)."""

    def __init__(self, host: str = "", port: int = 587, user: str = "", password: str = "", sender: str = "",
                 send_fn=None):
        self.host, self.port, self.user, self.password, self.sender = host, port, user, password, sender
        self._send_fn = send_fn  # tests inject a fake
        self.sent: list[EmailMessage] = []

    @property
    def configured(self) -> bool:
        return bool(self._send_fn or (self.host and self.sender))

    def _deliver(self, msg: EmailMessage) -> None:
        try:
            if self._send_fn:
                self._send_fn(msg)
            else:
                with smtplib.SMTP(self.host, self.port, timeout=15) as smtp:
                    smtp.starttls(context=ssl.create_default_context())
                    if self.user:
                        smtp.login(self.user, self.password)
                    smtp.send_message(msg)
            self.sent.append(msg)
        except Exception as err:
            log.warning("Invoice e-mail to %s failed: %s", msg["To"], err)

    def send_invoice(self, to: str, store: dict, number: str, total_paise: int, html_body: str, link: str,
                     background: bool = True) -> None:
        if not self.configured:
            return
        msg = EmailMessage()
        msg["Subject"] = f"Your {store['trade_name']} invoice {number}"
        msg["From"] = self.sender or "receipts@trustlane.invalid"
        msg["To"] = to
        msg.set_content(f"Thanks for shopping at {store['trade_name']}.\n\nInvoice {number}, total "
                        f"Rs {total_paise / 100:.2f}.\nView or print it: {link}\n")
        msg.add_alternative(html_body, subtype="html")
        if background:
            threading.Thread(target=self._deliver, args=(msg,), daemon=True).start()
        else:
            self._deliver(msg)


class Accounts:
    def __init__(self, ledger, sms: SmsSender, mailer: Mailer):
        self.ledger, self.sms, self.mailer = ledger, sms, mailer
        self._lock = threading.Lock()

    def start_otp(self, phone: str, now: float) -> str:
        phone = normalise_phone(phone)
        code = f"{secrets.randbelow(10 ** 6):06d}"
        self.ledger.save_otp(phone, _hash_code(phone, code), now + OTP_TTL_S)
        self.sms.send(phone, f"{code} is your TrustLane sign-in code. It expires in 5 minutes. Don't share it.")
        return phone

    def verify_otp(self, phone: str, code: str, current: tuple[str, str] | None, now: float) -> dict:
        """Proves the phone number. Returns this person's shopper id and a secret for this device.

        If the number already has an account, this phone joins it (its trips so far stay with the
        anonymous id it had). Otherwise the phone's current anonymous shopper becomes the account."""
        phone = normalise_phone(phone)
        with self._lock:
            row = self.ledger.take_otp(phone)
            if not row:
                raise AccountError(400, "Ask for a new code")
            code_hash, expires, attempts = row
            if now > expires or attempts >= OTP_MAX_ATTEMPTS:
                self.ledger.clear_otp(phone)
                raise AccountError(400, "That code expired. Ask for a new one.")
            if not hmac.compare_digest(code_hash, _hash_code(phone, (code or "").strip())):
                raise AccountError(400, "That code isn't right")
            self.ledger.clear_otp(phone)
            existing = self.ledger.shopper_by_phone(phone)
            if existing:
                return {"shopper_id": existing, "shopper_secret": self.ledger.add_device(existing, now), "new": False}
            if current and self.ledger.check_shopper(*current):
                shopper_id, secret = current
            else:
                shopper_id, secret = self.ledger.register_shopper()
            self.ledger.save_profile(shopper_id, now, phone=phone)
            return {"shopper_id": shopper_id, "shopper_secret": secret, "new": True}

    def check(self, shopper_id: str, secret: str) -> None:
        if not self.ledger.check_shopper(shopper_id, secret):
            raise AccountError(401, "Sign in again")

    def me(self, shopper_id: str) -> dict:
        profile = self.ledger.profile(shopper_id) or {}
        return {"shopper_id": shopper_id, "phone": profile.get("phone"), "email": profile.get("email"),
                "name": profile.get("name"), "points": self.ledger.points(shopper_id),
                "receipts": self.ledger.invoices_for(shopper_id), "email_receipts": self.mailer.configured}

    def update(self, shopper_id: str, name: str | None, email: str | None, now: float) -> None:
        fields = {}
        if name is not None:
            fields["name"] = name.strip()[:80] or None
        if email is not None:
            email = email.strip()
            if email and not EMAIL_RE.match(email):
                raise AccountError(400, "That e-mail address doesn't look right")
            fields["email"] = email or None
        self.ledger.save_profile(shopper_id, now, **fields)

    # -- DPDP Act 2023: access and erasure
    def export(self, shopper_id: str, trips: list[dict], mandate: dict | None) -> dict:
        return {
            "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "account": self.me(shopper_id),
            "invoices": [self.ledger.get_invoice(i["number"]) for i in self.ledger.invoices_for(shopper_id, 1000)],
            "trips": trips,
            "upi_reserve_pay": mandate,
            "what_we_keep": "Invoices are kept 72 months (GST law). Shelf events and the audit log hold no "
                            "personal data. Camera video never leaves the shelf device.",
        }

    def erase(self, shopper_id: str) -> dict:
        return self.ledger.erase_shopper(shopper_id)
