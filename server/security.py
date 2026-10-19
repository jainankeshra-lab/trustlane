"""Signatures: phone scans and requests (HMAC per trip), Bluetooth presence IDs, staff PIN,
and exit tokens (Ed25519, verifiable offline with only the public key)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

REQUEST_SKEW_S = 300  # how far a phone's clock may drift from the server's
EID_PERIOD_S = 30     # the phone's Bluetooth ID rotates this often


def new_session_id() -> str:
    """Unguessable trip id (it appears in URLs and on the gate screen)."""
    return f"T{secrets.token_hex(5).upper()}"


def new_session_key() -> str:
    return secrets.token_hex(32)


def _mac(key_hex: str, message: str) -> str:
    return hmac.new(bytes.fromhex(key_hex), message.encode(), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------- scans
def scan_message(session_id: str, seq: int, barcode: str, client_ts: int) -> bytes:
    return f"{session_id}|{seq}|{barcode}|{client_ts}".encode()


def sign_scan(key_hex: str, session_id: str, seq: int, barcode: str, client_ts: int) -> str:
    return _mac(key_hex, scan_message(session_id, seq, barcode, client_ts).decode())


def verify_scan_signature(key_hex: str, session_id: str, seq: int, barcode: str, client_ts: int, sig: str) -> bool:
    return hmac.compare_digest(sign_scan(key_hex, session_id, seq, barcode, client_ts), sig or "")


# ---------------------------------------------------------------- every other trip request
def sign_request(key_hex: str, method: str, path: str, ts_ms: int) -> str:
    """Header value for X-Trip-Auth: "<ts_ms>.<hmac(key, METHOD|path|ts_ms)>"."""
    return f"{ts_ms}.{_mac(key_hex, f'{method.upper()}|{path}|{ts_ms}')}"


def verify_request(key_hex: str, method: str, path: str, header: str | None, now: float) -> bool:
    try:
        ts_s, sig = (header or "").split(".", 1)
        ts_ms = int(ts_s)
    except ValueError:
        return False
    if abs(now - ts_ms / 1000) > REQUEST_SKEW_S:
        return False
    return hmac.compare_digest(sign_request(key_hex, method, path, ts_ms), f"{ts_ms}.{sig}")


# ---------------------------------------------------------------- Bluetooth presence
def eid_for(key_hex: str, now: float) -> str:
    """Rotating Bluetooth ID the phone advertises (like Eddystone-EID). Only the server can map it
    back to a trip, and it changes every EID_PERIOD_S, so it can't be used to track the shopper."""
    return _mac(key_hex, f"eid|{int(now // EID_PERIOD_S)}")[:16]


def eid_matches(key_hex: str, eid: str, now: float) -> bool:
    return any(hmac.compare_digest(eid_for(key_hex, now - k * EID_PERIOD_S), eid) for k in (0, 1))


# ---------------------------------------------------------------- staff
def staff_ok(expected_pin: str, given: str | None) -> bool:
    return bool(expected_pin) and hmac.compare_digest(expected_pin, given or "")


# ---------------------------------------------------------------- exit tokens
def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class TokenError(Exception):
    pass


class TokenSigner:
    """Signs exit tokens. The gate needs only the public key, so it can verify with no network."""

    def __init__(self, key_path: Path):
        key_path = Path(key_path)
        if key_path.exists():
            self._key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        else:
            self._key = Ed25519PrivateKey.generate()
            key_path.parent.mkdir(parents=True, exist_ok=True)
            key_path.write_bytes(self._key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        self.public_key: Ed25519PublicKey = self._key.public_key()

    @property
    def public_key_b64(self) -> str:
        raw = self.public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return _b64e(raw)

    def sign(self, payload: dict) -> str:
        body = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
        return f"{body}.{_b64e(self._key.sign(body.encode()))}"


def verify_token(token: str, public_key: Ed25519PublicKey, now: float, grace_s: float = 0) -> dict:
    """grace_s: accept a token up to this long past expiry (a gate syncing exits it let through offline)."""
    try:
        body, sig = token.split(".")
        public_key.verify(_b64d(sig), body.encode())
        payload = json.loads(_b64d(body))
    except (ValueError, InvalidSignature) as exc:
        raise TokenError("token signature is not valid") from exc
    if now > payload.get("exp", 0) + grace_s:
        raise TokenError("token has expired")
    return payload


def cart_hash(lines: list[tuple[str, str, int]]) -> str:
    """Hash of (line_id, sku, price) so the gate can tell if the basket changed after the token."""
    return hashlib.sha256(json.dumps(sorted(lines)).encode()).hexdigest()[:16]
