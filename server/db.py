"""A SQLite connection that is safe to share between threads.

The web server answers some reads (a phone's cart, the ops console) on worker threads while engine
calls write on others. Python's sqlite3 connection must not be used by two threads at the same time,
so every statement runs under one lock and its rows are read before the lock is released.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path


class Rows:
    """The fully fetched result of one statement (what a cursor would have given)."""

    def __init__(self, rows: list, rowcount: int, lastrowid: int | None):
        self._rows, self.rowcount, self.lastrowid = rows, rowcount, lastrowid

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list:
        return list(self._rows)

    def __iter__(self):
        return iter(self._rows)


class LockedConnection:
    def __init__(self, path: Path | str):
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.RLock()

    def execute(self, sql: str, params=()) -> Rows:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return Rows(cur.fetchall(), cur.rowcount, cur.lastrowid)

    def executescript(self, script: str) -> None:
        with self._lock:
            self._conn.executescript(script)

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

    # `with db:` is a transaction, held under the lock from start to commit/rollback.
    def __enter__(self):
        self._lock.acquire()
        self._conn.__enter__()
        return self

    def __exit__(self, *exc):
        try:
            return self._conn.__exit__(*exc)
        finally:
            self._lock.release()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
