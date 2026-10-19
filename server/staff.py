"""Staff accounts: per-person logins with roles, so every staff action is attributed to someone.

Roles, each including the ones before it:
  associate   watch the console, clear red checks, verify ID, label camera photos
  supervisor  refunds, close a trip at the desk, cancel trips, resolve disputes, prices, retraining,
              network switch, audit-log check
  admin       manage staff accounts

Passwords are stored as scrypt hashes. A login returns a signed bearer token (12 hours). The
setup PIN (TRUSTLANE_STAFF_PIN, or data/staff_pin.txt) still works as a break-glass admin login so the
first accounts can be created; `python -m server.staff add <username> <role>` also creates one.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .db import LockedConnection

ROLES = ("associate", "supervisor", "admin")
TOKEN_TTL_S = 12 * 3600
USERNAME = re.compile(r"^[a-z0-9._-]{2,32}$")


class StaffError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass
class StaffUser:
    username: str
    name: str
    role: str

    def can(self, role: str) -> bool:
        return ROLES.index(self.role) >= ROLES.index(role)

    def public(self) -> dict:
        return {"username": self.username, "name": self.name, "role": self.role}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
    except ValueError:
        return False
    calc = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1, dklen=32)
    return hmac.compare_digest(calc.hex(), digest)


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class StaffDirectory:
    def __init__(self, data_dir: Path):
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        self._db = LockedConnection(str(data_dir / "staff.sqlite3"))
        self._db.execute("""CREATE TABLE IF NOT EXISTS staff (
            username TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL, pw_hash TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL, token_epoch INTEGER NOT NULL DEFAULT 0)""")
        self._db.commit()
        self._lock = threading.Lock()
        key_path = data_dir / "staff_token_key"
        if not key_path.exists():
            key_path.write_text(secrets.token_hex(32))
        self._key = bytes.fromhex(key_path.read_text().strip())

    # -- accounts
    def add(self, username: str, name: str, role: str, password: str, now: float) -> StaffUser:
        username = username.strip().lower()
        if not USERNAME.match(username):
            raise StaffError(400, "Username: 2-32 of a-z, 0-9, dot, dash, underscore")
        if role not in ROLES:
            raise StaffError(400, f"Role must be one of {', '.join(ROLES)}")
        if len(password) < 8:
            raise StaffError(400, "Password must be at least 8 characters")
        with self._lock:
            try:
                self._db.execute("INSERT INTO staff (username, name, role, pw_hash, created) VALUES (?,?,?,?,?)",
                                 (username, name.strip() or username, role, hash_password(password), now))
                self._db.commit()
            except sqlite3.IntegrityError as err:
                raise StaffError(409, "That username is taken") from err
        return StaffUser(username, name.strip() or username, role)

    def update(self, username: str, *, role: str | None = None, active: bool | None = None,
               password: str | None = None) -> None:
        if role is not None and role not in ROLES:
            raise StaffError(400, f"Role must be one of {', '.join(ROLES)}")
        if password is not None and len(password) < 8:
            raise StaffError(400, "Password must be at least 8 characters")
        with self._lock:
            if not self._db.execute("SELECT 1 FROM staff WHERE username=?", (username,)).fetchone():
                raise StaffError(404, "No such staff account")
            if role is not None:
                self._db.execute("UPDATE staff SET role=?, token_epoch=token_epoch+1 WHERE username=?", (role, username))
            if active is not None:
                self._db.execute("UPDATE staff SET active=?, token_epoch=token_epoch+1 WHERE username=?",
                                 (int(active), username))
            if password is not None:
                self._db.execute("UPDATE staff SET pw_hash=?, token_epoch=token_epoch+1 WHERE username=?",
                                 (hash_password(password), username))
            self._db.commit()

    def list(self) -> list[dict]:
        rows = self._db.execute("SELECT username, name, role, active, created FROM staff ORDER BY username").fetchall()
        return [{"username": u, "name": n, "role": r, "active": bool(a), "created": c} for u, n, r, a, c in rows]

    def count(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM staff").fetchone()[0]

    def _row(self, username: str):
        return self._db.execute("SELECT name, role, pw_hash, active, token_epoch FROM staff WHERE username=?",
                                (username,)).fetchone()

    # -- login and tokens
    def login(self, username: str, password: str, now: float) -> tuple[str, StaffUser]:
        username = (username or "").strip().lower()
        row = self._row(username)
        # Hash even when the user doesn't exist, so timing doesn't reveal valid usernames.
        ok = check_password(password or "", row[2] if row else hash_password("x"))
        if not row or not ok or not row[3]:
            raise StaffError(401, "Wrong username or password")
        user = StaffUser(username, row[0], row[1])
        body = _b64(json.dumps({"u": username, "e": row[4], "x": int(now + TOKEN_TTL_S)}).encode())
        sig = _b64(hmac.new(self._key, body.encode(), hashlib.sha256).digest())
        return f"{body}.{sig}", user

    def verify(self, token: str, now: float) -> StaffUser:
        try:
            body, sig = token.split(".")
            expected = _b64(hmac.new(self._key, body.encode(), hashlib.sha256).digest())
            if not hmac.compare_digest(expected, sig):
                raise ValueError
            data = json.loads(_unb64(body))
        except (ValueError, json.JSONDecodeError) as err:
            raise StaffError(401, "Staff sign-in is not valid") from err
        if now > data["x"]:
            raise StaffError(401, "Staff sign-in expired: sign in again")
        row = self._row(data["u"])
        # Changing a role, password or disabling an account bumps the epoch, ending existing sign-ins.
        if not row or not row[3] or row[4] != data["e"]:
            raise StaffError(401, "Staff sign-in is no longer valid: sign in again")
        return StaffUser(data["u"], row[0], row[1])


def main() -> None:
    from .config import Settings

    ap = argparse.ArgumentParser(description="TrustLane staff accounts")
    sub = ap.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add")
    add.add_argument("username")
    add.add_argument("role", choices=ROLES)
    add.add_argument("--name", default="")
    sub.add_parser("list")
    args = ap.parse_args()
    d = StaffDirectory(Settings().data_dir)
    if args.cmd == "add":
        pw = getpass.getpass("Password (8+ characters): ")
        if pw != getpass.getpass("Again: "):
            raise SystemExit("Passwords don't match")
        try:
            print("Created", d.add(args.username, args.name or args.username, args.role, pw, time.time()).public())
        except StaffError as err:
            raise SystemExit(str(err)) from err
    else:
        for u in d.list():
            print(f"{u['username']:<20} {u['role']:<11} {'active' if u['active'] else 'disabled':<9} {u['name']}")


if __name__ == "__main__":
    main()
