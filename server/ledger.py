"""SQLite storage: the append-only audit ledger, shopper credentials, and the live-state snapshot.

Audit ledger: each row carries the hash of the row before it, so any edit or deletion breaks the
chain and `verify_chain()` reports it. Retention prunes old rows but keeps an anchor (the last
pruned row's hash), so the remaining chain still verifies.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from pathlib import Path

GENESIS = "0" * 64


class Ledger:
    def __init__(self, path: Path | str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL, kind TEXT NOT NULL, session_id TEXT,
                payload TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS shoppers (
                shopper_id TEXT PRIMARY KEY, clean_streak INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS anchor (
                id INTEGER PRIMARY KEY CHECK (id = 1), last_id INTEGER NOT NULL, last_hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS snapshot (
                id INTEGER PRIMARY KEY CHECK (id = 1), ts REAL NOT NULL, data TEXT NOT NULL);
            """
        )
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(shoppers)")}
        if "secret_hash" not in cols:  # migrate databases from before shopper credentials
            self._db.execute("ALTER TABLE shoppers ADD COLUMN secret_hash TEXT")
        row = self._db.execute("SELECT hash FROM events ORDER BY id DESC LIMIT 1").fetchone()
        self._last = row[0] if row else self._anchor()[1]

    # ---------------------------------------------------------------- audit chain
    def _anchor(self) -> tuple[int, str]:
        row = self._db.execute("SELECT last_id, last_hash FROM anchor WHERE id = 1").fetchone()
        return (row[0], row[1]) if row else (0, GENESIS)

    @staticmethod
    def _hash(prev: str, ts: float, kind: str, session_id: str | None, payload: str) -> str:
        return hashlib.sha256(f"{prev}|{ts:.3f}|{kind}|{session_id or ''}|{payload}".encode()).hexdigest()

    def append(self, ts: float, kind: str, session_id: str | None, payload: dict) -> str:
        body = json.dumps(payload, sort_keys=True, default=str)
        digest = self._hash(self._last, ts, kind, session_id, body)
        self._db.execute(
            "INSERT INTO events (ts, kind, session_id, payload, prev_hash, hash) VALUES (?,?,?,?,?,?)",
            (ts, kind, session_id, body, self._last, digest),
        )
        self._db.commit()
        self._last = digest
        return digest

    def verify_chain(self) -> bool:
        _, prev = self._anchor()
        for ts, kind, sid, body, prev_hash, digest in self._db.execute(
            "SELECT ts, kind, session_id, payload, prev_hash, hash FROM events ORDER BY id"
        ):
            if prev_hash != prev or self._hash(prev, ts, kind, sid, body) != digest:
                return False
            prev = digest
        return True

    def prune(self, older_than: float) -> int:
        """Delete audit rows older than `older_than`, keeping the chain verifiable via the anchor."""
        row = self._db.execute("SELECT id, hash FROM events WHERE ts < ? ORDER BY id DESC LIMIT 1",
                               (older_than,)).fetchone()
        if not row:
            return 0
        last_id, last_hash = row
        removed = self._db.execute("DELETE FROM events WHERE id <= ?", (last_id,)).rowcount
        self._db.execute("INSERT INTO anchor (id, last_id, last_hash) VALUES (1, ?, ?) "
                         "ON CONFLICT(id) DO UPDATE SET last_id=excluded.last_id, last_hash=excluded.last_hash",
                         (last_id, last_hash))
        self._db.commit()
        return removed

    def count(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    def sales(self) -> list[tuple]:
        return self._db.execute(
            "SELECT ts, session_id, payload FROM events WHERE kind = 'exit.paid' ORDER BY id").fetchall()

    # ---------------------------------------------------------------- shoppers
    def register_shopper(self) -> tuple[str, str]:
        """New shopper identity. The secret stays on the phone; only its hash is stored."""
        shopper_id, secret = f"S{secrets.token_hex(6)}", secrets.token_urlsafe(24)
        self._db.execute("INSERT INTO shoppers (shopper_id, clean_streak, secret_hash) VALUES (?, 0, ?)",
                         (shopper_id, hashlib.sha256(secret.encode()).hexdigest()))
        self._db.commit()
        return shopper_id, secret

    def check_shopper(self, shopper_id: str | None, secret: str | None) -> bool:
        if not shopper_id or not secret:
            return False
        row = self._db.execute("SELECT secret_hash FROM shoppers WHERE shopper_id=?", (shopper_id,)).fetchone()
        return bool(row and row[0]) and hmac.compare_digest(row[0], hashlib.sha256(secret.encode()).hexdigest())

    def clean_streak(self, shopper_id: str) -> int:
        row = self._db.execute("SELECT clean_streak FROM shoppers WHERE shopper_id=?", (shopper_id,)).fetchone()
        return row[0] if row else 0

    def set_clean_streak(self, shopper_id: str, streak: int) -> None:
        self._db.execute(
            "INSERT INTO shoppers (shopper_id, clean_streak) VALUES (?,?) "
            "ON CONFLICT(shopper_id) DO UPDATE SET clean_streak=excluded.clean_streak",
            (shopper_id, streak),
        )
        self._db.commit()

    # ---------------------------------------------------------------- live state
    def save_snapshot(self, ts: float, state: dict) -> None:
        self._db.execute("INSERT INTO snapshot (id, ts, data) VALUES (1, ?, ?) "
                         "ON CONFLICT(id) DO UPDATE SET ts=excluded.ts, data=excluded.data",
                         (ts, json.dumps(state, default=str)))
        self._db.commit()

    def load_snapshot(self) -> dict | None:
        row = self._db.execute("SELECT data FROM snapshot WHERE id = 1").fetchone()
        return json.loads(row[0]) if row else None
