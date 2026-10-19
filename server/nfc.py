"""NFC tap at the exit gate: NTAG 424 DNA tags with Secure Unique NFC (SUN) messages.

Each gate carries an NTAG 424 DNA sticker. Every time a phone reads it, the tag itself produces a new
URL like  https://<store>/g?picc_data=<32 hex>&cmac=<16 hex>:

  * picc_data = AES-128-CBC(meta key, IV 0) of  tag | UID (7 bytes) | read counter (3 bytes, LSB first) | pad
  * cmac      = CMAC with a session key derived from the file key, the UID and the counter, truncated
                to its odd bytes (NXP AN12196, "SUN message" with no mirrored data before the MAC).

The keys never leave the tag, so a valid message proves a phone touched that tag, and the counter
must go up on every read, so a captured URL can't be replayed. The tag's UID tells us which gate it is,
so a phone can't claim a different gate.

`python -m server.nfc init` makes keys and a tag registry (data/nfc_tags.json); `add-tag` registers a
tag's UID for a gate; `simulate` produces valid messages for a registered tag, for testing without
hardware. Program real tags with NXP TagXplorer or TagWriter (SDM on, the URL template printed by init).
"""
from __future__ import annotations

import argparse
import json
import secrets
import threading
from pathlib import Path

from cryptography.hazmat.primitives import cmac
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .db import LockedConnection


class SunError(Exception):
    pass


def _cmac(key: bytes, data: bytes) -> bytes:
    c = cmac.CMAC(algorithms.AES(key))
    c.update(data)
    return c.finalize()


def _aes_cbc(key: bytes, data: bytes, decrypt: bool) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.CBC(bytes(16)))
    op = cipher.decryptor() if decrypt else cipher.encryptor()
    return op.update(data) + op.finalize()


def _sun_mac(file_key: bytes, uid: bytes, counter: bytes) -> bytes:
    sv2 = bytes.fromhex("3CC300010080") + uid + counter
    session_key = _cmac(file_key, sv2)
    full = _cmac(session_key, b"")
    return full[1::2]  # NXP truncation: the odd-numbered bytes


def decrypt_sun(picc_data_hex: str, cmac_hex: str, meta_key: bytes, file_key: bytes) -> tuple[str, int]:
    """Check one SUN message. Returns (UID hex, read counter) or raises SunError."""
    try:
        enc, mac = bytes.fromhex(picc_data_hex), bytes.fromhex(cmac_hex)
    except ValueError as err:
        raise SunError("NFC message is not hex") from err
    if len(enc) != 16 or len(mac) != 8:
        raise SunError("NFC message has the wrong length")
    plain = _aes_cbc(meta_key, enc, decrypt=True)
    tag = plain[0]
    if not (tag & 0x80 and tag & 0x40) or (tag & 0x0F) != 7:
        raise SunError("NFC tag is not set up to mirror its UID and read counter")
    uid, counter = plain[1:8], plain[8:11]
    if not secrets.compare_digest(_sun_mac(file_key, uid, counter), mac):
        raise SunError("NFC message signature doesn't match: not a genuine store tag")
    return uid.hex().upper(), int.from_bytes(counter, "little")


def make_sun(uid_hex: str, counter: int, meta_key: bytes, file_key: bytes) -> tuple[str, str]:
    """What a real tag computes on each read (used for tests and the simulator)."""
    uid, ctr = bytes.fromhex(uid_hex), counter.to_bytes(3, "little")
    plain = bytes([0xC7]) + uid + ctr + secrets.token_bytes(5)
    return _aes_cbc(meta_key, plain, decrypt=False).hex().upper(), _sun_mac(file_key, uid, ctr).hex().upper()


class GateTags:
    """Registry of the store's gate tags (data/nfc_tags.json) and the last counter seen per tag."""

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "nfc_tags.json"
        self._db = LockedConnection(Path(data_dir) / "nfc_counters.sqlite3")
        self._db.execute("CREATE TABLE IF NOT EXISTS counters (uid TEXT PRIMARY KEY, counter INTEGER NOT NULL)")
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return self.path.exists()

    def _config(self) -> dict:
        if not self.configured:
            raise SunError("This store has no NFC gate tags set up")
        return json.loads(self.path.read_text(encoding="utf-8"))

    def verify(self, picc_data: str, cmac_hex: str) -> tuple[str, str, int]:
        """Returns (gate id, UID, counter) for a fresh, genuine message from a registered tag."""
        cfg = self._config()
        uid, counter = decrypt_sun(picc_data, cmac_hex, bytes.fromhex(cfg["meta_key"]), bytes.fromhex(cfg["file_key"]))
        gate = cfg["tags"].get(uid)
        if not gate:
            raise SunError("This NFC tag isn't registered to a gate")
        with self._lock:
            row = self._db.execute("SELECT counter FROM counters WHERE uid=?", (uid,)).fetchone()
            if row and counter <= row[0]:
                raise SunError("This NFC read was already used: tap the gate again")
            self._db.execute("INSERT INTO counters (uid, counter) VALUES (?, ?) "
                             "ON CONFLICT(uid) DO UPDATE SET counter=excluded.counter", (uid, counter))
            self._db.commit()
        return gate, uid, counter


# ---------------------------------------------------------------- command line
def main() -> None:
    from .config import Settings

    ap = argparse.ArgumentParser(description="NFC gate tags (NTAG 424 DNA SUN)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    init = sub.add_parser("init", help="create keys and an empty tag registry")
    init.add_argument("--url", default="https://<store-server>/g", help="where the tag's URL points")
    add = sub.add_parser("add-tag", help="register a tag UID for a gate")
    add.add_argument("uid", help="7-byte UID in hex, e.g. 04DE5F1EACC040")
    add.add_argument("gate", help="gate id, e.g. G1")
    sim = sub.add_parser("simulate", help="print a valid tap URL for a registered tag (testing)")
    sim.add_argument("gate")
    sim.add_argument("--counter", type=int, default=None)
    args = ap.parse_args()

    data = Path(Settings().data_dir)
    data.mkdir(parents=True, exist_ok=True)
    path = data / "nfc_tags.json"
    if args.cmd == "init":
        if path.exists():
            raise SystemExit(f"{path} already exists; delete it first to make new keys")
        cfg = {"meta_key": secrets.token_hex(16).upper(), "file_key": secrets.token_hex(16).upper(), "tags": {}}
        path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        print(f"Keys written to {path} (keep it secret).")
        print("Program each NTAG 424 DNA with NXP TagXplorer / TagWriter:")
        print(f"  NDEF URL: {args.url}?picc_data=00000000000000000000000000000000&cmac=0000000000000000")
        print("  SDM on, UID + read counter mirrored, encrypted PICC data at the picc_data offset,")
        print(f"  SDM meta read key  (key 1) = {cfg['meta_key']}")
        print(f"  SDM file read key  (key 2) = {cfg['file_key']}")
        print("  SDM MAC input offset = SDM MAC offset (MAC over no data).")
        return
    cfg = json.loads(path.read_text(encoding="utf-8"))
    if args.cmd == "add-tag":
        cfg["tags"][args.uid.upper()] = args.gate
        path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        print(f"Tag {args.uid.upper()} -> gate {args.gate}")
    elif args.cmd == "simulate":
        uid = next((u for u, g in cfg["tags"].items() if g == args.gate), None)
        if not uid:
            raise SystemExit(f"No tag registered for gate {args.gate}")
        counter = args.counter if args.counter is not None else int.from_bytes(secrets.token_bytes(2), "big") + 1000
        picc, mac = make_sun(uid, counter, bytes.fromhex(cfg["meta_key"]), bytes.fromhex(cfg["file_key"]))
        print(f"/g?picc_data={picc}&cmac={mac}")


if __name__ == "__main__":
    main()
