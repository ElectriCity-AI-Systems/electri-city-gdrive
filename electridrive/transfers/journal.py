"""Account-scoped durable transfer payloads; interrupted work is never replayed."""
from __future__ import annotations

from electridrive import qa_policy as qa

import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any


class TransferJournal:
    def __init__(self, path: Path, account_key: str):
        qa.check_startup()
        if qa.current_policy():
            raise qa.QaSafetyError("QA safety: filesystem transfer journals are disabled")
        if not account_key or account_key.startswith("ephemeral:"):
            raise ValueError("A stable account identity is required for saved transfers")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.account_key = account_key
        self._lock = threading.RLock()
        self._closed = False
        scope = hashlib.sha256(account_key.encode()).hexdigest()
        lock_path = self.path.with_name(f"{self.path.name}.{scope}.lock")
        self._lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(self._lock_fd)
            raise RuntimeError("Another transfer queue is already open for this account") from exc
        try:
            self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS transfer_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_key TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
            """)
            self._conn.execute("CREATE INDEX IF NOT EXISTS transfer_jobs_account ON transfer_jobs(account_key)")
            self._conn.commit()
        except Exception:
            if hasattr(self, '_conn'):
                self._conn.close()
            os.close(self._lock_fd)
            raise

    def allocate(self, payload: dict[str, Any]) -> int:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT INTO transfer_jobs(account_key,payload) VALUES (?,?)",
                (self.account_key, json.dumps(payload)),
            )
            return int(cursor.lastrowid)

    def save(self, transfer_id: int, payload: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE transfer_jobs SET payload=? WHERE id=? AND account_key=?",
                (json.dumps(payload), transfer_id, self.account_key),
            )

    def load(self) -> list[tuple[int, dict[str, Any]]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id,payload FROM transfer_jobs WHERE account_key=? ORDER BY id",
                (self.account_key,),
            ).fetchall()
        return [(row[0], json.loads(row[1])) for row in rows]

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._conn.close()
            os.close(self._lock_fd)
            self._closed = True
