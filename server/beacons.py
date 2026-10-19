"""Shelf tokens: shelves broadcast a short token that changes every 30 s; phones report what they hear.

This is the other direction from the phone's rotating Bluetooth ID (which shelves listen for). It
works where that can't: iPhones won't advertise custom data from the background, but they can listen
for a known service in the background. A token is HMAC(shelf key, window)[:4 bytes], so a phone can't
make one up; hearing a valid one means the phone was within Bluetooth range of that shelf just now.

Each shelf has its own key, derived from a store secret (data/beacon_key) and its shelf id, and a
small number (1-65535) that fits in the advertisement. Stations fetch both with their station key.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
from pathlib import Path

WINDOW_S = 30


def token_for(shelf_key: bytes, now: float) -> str:
    window = int(now // WINDOW_S)
    return hmac.new(shelf_key, f"beacon|{window}".encode(), hashlib.sha256).hexdigest()[:8]


class ShelfBeacons:
    def __init__(self, data_dir: Path):
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        key_path = data_dir / "beacon_key"
        if not key_path.exists():
            key_path.write_text(secrets.token_hex(32))
        self._secret = bytes.fromhex(key_path.read_text().strip())
        self._map_path = data_dir / "beacon_shelves.json"
        self._numbers: dict[str, int] = json.loads(self._map_path.read_text()) if self._map_path.exists() else {}
        self._lock = threading.Lock()

    def shelf_key(self, shelf_id: str) -> bytes:
        return hmac.new(self._secret, f"shelf|{shelf_id}".encode(), hashlib.sha256).digest()[:16]

    def number(self, shelf_id: str) -> int:
        with self._lock:
            if shelf_id not in self._numbers:
                self._numbers[shelf_id] = max(self._numbers.values(), default=0) + 1
                self._map_path.write_text(json.dumps(self._numbers, indent=1))
            return self._numbers[shelf_id]

    def config(self, shelf_id: str) -> dict:
        return {"shelf_id": shelf_id, "shelf_no": self.number(shelf_id), "key": self.shelf_key(shelf_id).hex(),
                "window_s": WINDOW_S}

    def verify(self, shelf_no: int, token: str, now: float) -> str | None:
        """The shelf id if `token` is that shelf's current (or previous, for clock skew) token."""
        shelf_id = next((s for s, n in self._numbers.items() if n == shelf_no), None)
        if not shelf_id or not isinstance(token, str) or len(token) != 8:
            return None
        key = self.shelf_key(shelf_id)
        for t in (now, now - WINDOW_S, now + WINDOW_S):
            if hmac.compare_digest(token_for(key, t), token.lower()):
                return shelf_id
        return None
