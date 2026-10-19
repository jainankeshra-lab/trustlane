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
from pathlib import Path

from .db import LockedConnection

GENESIS = "0" * 64


class Ledger:
    def __init__(self, path: Path | str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = LockedConnection(str(path))
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
            CREATE TABLE IF NOT EXISTS mandates (
                subscription_id TEXT PRIMARY KEY, shopper_id TEXT NOT NULL, status TEXT NOT NULL,
                created REAL NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS invoices (
                number TEXT PRIMARY KEY, kind TEXT NOT NULL, fy TEXT NOT NULL, seq INTEGER NOT NULL,
                session_id TEXT, shopper_id TEXT, ts REAL NOT NULL, total INTEGER NOT NULL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS invoices_shopper ON invoices (shopper_id, ts);
            CREATE TABLE IF NOT EXISTS loyalty (shopper_id TEXT PRIMARY KEY, points INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS coupon_uses (shopper_id TEXT NOT NULL, code TEXT NOT NULL, ts REAL NOT NULL,
                PRIMARY KEY (shopper_id, code));
            CREATE TABLE IF NOT EXISTS profiles (
                shopper_id TEXT PRIMARY KEY, phone TEXT UNIQUE, email TEXT, name TEXT, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS devices (
                shopper_id TEXT NOT NULL, secret_hash TEXT NOT NULL PRIMARY KEY, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS trips_archive (
                session_id TEXT PRIMARY KEY, shopper_id TEXT, ended REAL NOT NULL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS trips_archive_shopper ON trips_archive (shopper_id);
            CREATE TABLE IF NOT EXISTS otps (
                phone TEXT PRIMARY KEY, code_hash TEXT NOT NULL, expires REAL NOT NULL, attempts INTEGER NOT NULL);
            """
        )
        self._db.execute("PRAGMA journal_mode=WAL")    # readers don't block the writer
        self._db.execute("PRAGMA busy_timeout=5000")
        # WAL + NORMAL: commits don't wait for a disk flush each time; the database stays consistent after a
        # crash, and at worst a power cut loses the last moment of events (SQLite's recommended WAL setting).
        self._db.execute("PRAGMA synchronous=NORMAL")
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
        """The shopper's first device secret, or one added by signing in on another phone."""
        if not shopper_id or not secret:
            return False
        digest = hashlib.sha256(secret.encode()).hexdigest()
        row = self._db.execute("SELECT secret_hash FROM shoppers WHERE shopper_id=?", (shopper_id,)).fetchone()
        if row and row[0] and hmac.compare_digest(row[0], digest):
            return True
        dev = self._db.execute("SELECT shopper_id FROM devices WHERE secret_hash=?", (digest,)).fetchone()
        return bool(dev) and dev[0] == shopper_id

    def add_device(self, shopper_id: str, now: float) -> str:
        secret = secrets.token_urlsafe(24)
        self._db.execute("INSERT INTO devices (shopper_id, secret_hash, created) VALUES (?,?,?)",
                         (shopper_id, hashlib.sha256(secret.encode()).hexdigest(), now))
        self._db.commit()
        return secret

    # ---------------------------------------------------------------- shopper accounts (phone sign-in)
    def profile(self, shopper_id: str) -> dict | None:
        row = self._db.execute("SELECT phone, email, name, created FROM profiles WHERE shopper_id=?", (shopper_id,)).fetchone()
        return dict(zip(("phone", "email", "name", "created"), row)) if row else None

    def shopper_by_phone(self, phone: str) -> str | None:
        row = self._db.execute("SELECT shopper_id FROM profiles WHERE phone=?", (phone,)).fetchone()
        return row[0] if row else None

    def save_profile(self, shopper_id: str, now: float, **fields) -> None:
        self._db.execute("INSERT INTO profiles (shopper_id, created) VALUES (?, ?) ON CONFLICT(shopper_id) DO NOTHING",
                         (shopper_id, now))
        for key in ("phone", "email", "name"):
            if key in fields:
                self._db.execute(f"UPDATE profiles SET {key}=? WHERE shopper_id=?", (fields[key], shopper_id))
        self._db.commit()

    def save_otp(self, phone: str, code_hash: str, expires: float) -> None:
        self._db.execute("INSERT INTO otps (phone, code_hash, expires, attempts) VALUES (?,?,?,0) "
                         "ON CONFLICT(phone) DO UPDATE SET code_hash=excluded.code_hash, expires=excluded.expires, attempts=0",
                         (phone, code_hash, expires))
        self._db.commit()

    def take_otp(self, phone: str) -> tuple[str, float, int] | None:
        row = self._db.execute("SELECT code_hash, expires, attempts FROM otps WHERE phone=?", (phone,)).fetchone()
        if row:
            self._db.execute("UPDATE otps SET attempts=attempts+1 WHERE phone=?", (phone,))
            self._db.commit()
        return row

    def clear_otp(self, phone: str) -> None:
        self._db.execute("DELETE FROM otps WHERE phone=?", (phone,))
        self._db.commit()

    # ---------------------------------------------------------------- loyalty and coupons
    def points(self, shopper_id: str) -> int:
        row = self._db.execute("SELECT points FROM loyalty WHERE shopper_id=?", (shopper_id,)).fetchone()
        return row[0] if row else 0

    def add_points(self, shopper_id: str, delta: int) -> int:
        self._db.execute("INSERT INTO loyalty (shopper_id, points) VALUES (?, 0) ON CONFLICT(shopper_id) DO NOTHING",
                         (shopper_id,))
        self._db.execute("UPDATE loyalty SET points=MAX(0, points+?) WHERE shopper_id=?", (delta, shopper_id))
        self._db.commit()
        return self.points(shopper_id)

    def coupon_used(self, shopper_id: str, code: str) -> bool:
        return bool(self._db.execute("SELECT 1 FROM coupon_uses WHERE shopper_id=? AND code=?",
                                     (shopper_id, code)).fetchone())

    def use_coupon(self, shopper_id: str, code: str, now: float) -> None:
        self._db.execute("INSERT OR IGNORE INTO coupon_uses (shopper_id, code, ts) VALUES (?,?,?)", (shopper_id, code, now))
        self._db.commit()

    # ---------------------------------------------------------------- invoices and credit notes
    def save_invoice(self, kind: str, prefix: str, fy: str, session_id: str, shopper_id: str, ts: float,
                     total: int, data: dict) -> str:
        """Allocates the next number in this financial year's series, gap-free."""
        with self._db:
            seq = (self._db.execute("SELECT MAX(seq) FROM invoices WHERE kind=? AND fy=?", (kind, fy)).fetchone()[0] or 0) + 1
            number = f"{prefix}/{fy}/{seq:06d}"
            self._db.execute("INSERT INTO invoices (number, kind, fy, seq, session_id, shopper_id, ts, total, data) "
                             "VALUES (?,?,?,?,?,?,?,?,?)",
                             (number, kind, fy, seq, session_id, shopper_id, ts, total, json.dumps(data)))
        return number

    def get_invoice(self, number: str) -> dict | None:
        row = self._db.execute("SELECT number, kind, session_id, shopper_id, ts, total, data FROM invoices WHERE number=?",
                               (number,)).fetchone()
        if not row:
            return None
        return {"number": row[0], "kind": row[1], "session_id": row[2], "shopper_id": row[3], "ts": row[4],
                "total": row[5], "data": json.loads(row[6])}

    def invoices_for(self, shopper_id: str, limit: int = 50) -> list[dict]:
        rows = self._db.execute("SELECT number, kind, session_id, ts, total FROM invoices WHERE shopper_id=? "
                                "ORDER BY ts DESC LIMIT ?", (shopper_id, limit)).fetchall()
        return [dict(zip(("number", "kind", "session_id", "ts", "total"), r)) for r in rows]

    # ---------------------------------------------------------------- finished trips (out of live memory)
    def archive_trip(self, session_id: str, shopper_id: str, ended: float, data: dict) -> None:
        self._db.execute("INSERT INTO trips_archive (session_id, shopper_id, ended, data) VALUES (?,?,?,?) "
                         "ON CONFLICT(session_id) DO UPDATE SET data=excluded.data, ended=excluded.ended",
                         (session_id, shopper_id, ended, json.dumps(data, default=str)))
        self._db.commit()

    def load_trip(self, session_id: str) -> dict | None:
        row = self._db.execute("SELECT data FROM trips_archive WHERE session_id=?", (session_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def archived_trips_for(self, shopper_id: str) -> list[dict]:
        return [json.loads(r[0]) for r in self._db.execute(
            "SELECT data FROM trips_archive WHERE shopper_id=? ORDER BY ended DESC LIMIT 500", (shopper_id,))]

    # ---------------------------------------------------------------- privacy (DPDP): erase a shopper
    def erase_shopper(self, shopper_id: str) -> dict:
        """Deletes personal data. Invoices stay (GST law: 72 months) but are unlinked from the person."""
        pseudonym = "erased-" + hashlib.sha256(shopper_id.encode()).hexdigest()[:12]
        with self._db:
            counts = {
                "profile": self._db.execute("DELETE FROM profiles WHERE shopper_id=?", (shopper_id,)).rowcount,
                "devices": self._db.execute("DELETE FROM devices WHERE shopper_id=?", (shopper_id,)).rowcount,
                "loyalty": self._db.execute("DELETE FROM loyalty WHERE shopper_id=?", (shopper_id,)).rowcount,
                "coupons": self._db.execute("DELETE FROM coupon_uses WHERE shopper_id=?", (shopper_id,)).rowcount,
                "mandates": self._db.execute("DELETE FROM mandates WHERE shopper_id=?", (shopper_id,)).rowcount,
                "invoices_unlinked": self._db.execute("UPDATE invoices SET shopper_id=? WHERE shopper_id=?",
                                                      (pseudonym, shopper_id)).rowcount,
                "trips_unlinked": self._db.execute("UPDATE trips_archive SET shopper_id=? WHERE shopper_id=?",
                                                   (pseudonym, shopper_id)).rowcount,
            }
            self._db.execute("DELETE FROM shoppers WHERE shopper_id=?", (shopper_id,))
        return counts

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

    # ---------------------------------------------------------------- UPI Reserve Pay mandates
    def save_mandate(self, subscription_id: str, shopper_id: str, status: str, created: float, expires: float) -> None:
        self._db.execute("INSERT INTO mandates (subscription_id, shopper_id, status, created, expires) VALUES (?,?,?,?,?) "
                         "ON CONFLICT(subscription_id) DO UPDATE SET status=excluded.status",
                         (subscription_id, shopper_id, status, created, expires))
        self._db.commit()

    def set_mandate_status(self, subscription_id: str, status: str) -> bool:
        changed = self._db.execute("UPDATE mandates SET status=? WHERE subscription_id=?", (status, subscription_id)).rowcount
        self._db.commit()
        return bool(changed)

    def mandate_for(self, shopper_id: str, now: float) -> dict | None:
        """The shopper's newest unexpired mandate (any status)."""
        row = self._db.execute("SELECT subscription_id, status, created, expires FROM mandates WHERE shopper_id=? "
                               "AND expires > ? ORDER BY created DESC LIMIT 1", (shopper_id, now)).fetchone()
        return dict(zip(("subscription_id", "status", "created", "expires"), row)) if row else None

    # ---------------------------------------------------------------- live state
    def save_snapshot(self, ts: float, state: dict) -> None:
        self._db.execute("INSERT INTO snapshot (id, ts, data) VALUES (1, ?, ?) "
                         "ON CONFLICT(id) DO UPDATE SET ts=excluded.ts, data=excluded.data",
                         (ts, json.dumps(state, default=str)))
        self._db.commit()

    def load_snapshot(self) -> dict | None:
        row = self._db.execute("SELECT data FROM snapshot WHERE id = 1").fetchone()
        return json.loads(row[0]) if row else None
