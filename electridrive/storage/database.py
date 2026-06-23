from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from time import time


@dataclass(frozen=True)
class FileRecord:
    local_path: str
    remote_id: str
    remote_path: str
    size: int
    mtime_ns: int
    sha256: str
    status: str
    updated_at: float


@dataclass(frozen=True)
class SyncItem:
    """Last-synced state of one path in a two-way sync pair (for conflict detection)."""

    pair_id: str
    local_rel: str
    remote_id: str
    local_mtime_ns: int
    local_size: int
    sha256: str
    remote_modified: str
    remote_md5: str
    updated_at: float


@dataclass(frozen=True)
class TransferRecord:
    id: int
    kind: str
    name: str
    local_path: str
    remote_id: str
    status: str
    bytes_done: int
    total_bytes: int
    error: str
    created_at: float
    updated_at: float


class SyncDatabase:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the UI uses worker threads; we guard with a lock.
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._init_schema()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA busy_timeout=30000;

                CREATE TABLE IF NOT EXISTS files (
                    local_path TEXT PRIMARY KEY,
                    remote_id TEXT NOT NULL,
                    remote_path TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    status TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS folder_map (
                    local_rel_path TEXT PRIMARY KEY,
                    remote_path TEXT NOT NULL,
                    remote_id TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS app_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS change_tokens (
                    account_key TEXT PRIMARY KEY,
                    token TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sync_items (
                    pair_id TEXT NOT NULL,
                    local_rel TEXT NOT NULL,
                    remote_id TEXT NOT NULL,
                    local_mtime_ns INTEGER NOT NULL,
                    local_size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    remote_modified TEXT NOT NULL,
                    remote_md5 TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (pair_id, local_rel)
                );

                CREATE TABLE IF NOT EXISTS remote_nodes (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    mime_type TEXT,
                    size INTEGER,
                    modified_time TEXT,
                    parent_id TEXT,
                    is_folder INTEGER NOT NULL DEFAULT 0,
                    fetched_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_remote_nodes_parent ON remote_nodes(parent_id);

                CREATE TABLE IF NOT EXISTS transfers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL,
                    local_path TEXT NOT NULL DEFAULT '',
                    remote_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    bytes_done INTEGER NOT NULL DEFAULT 0,
                    total_bytes INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )
            self._conn.commit()

    # --------------------------------------------------------------- files index
    def get_file(self, local_path: str) -> FileRecord | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM files WHERE local_path=?", (local_path,)).fetchone()
        if row is None:
            return None
        return FileRecord(**dict(row))

    def upsert_file(
        self,
        *,
        local_path: str,
        remote_id: str,
        remote_path: str,
        size: int,
        mtime_ns: int,
        sha256: str,
        status: str,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO files(local_path, remote_id, remote_path, size, mtime_ns, sha256, status, updated_at)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(local_path) DO UPDATE SET
                    remote_id=excluded.remote_id,
                    remote_path=excluded.remote_path,
                    size=excluded.size,
                    mtime_ns=excluded.mtime_ns,
                    sha256=excluded.sha256,
                    status=excluded.status,
                    updated_at=excluded.updated_at
                """,
                (local_path, remote_id, remote_path, size, mtime_ns, sha256, status, time()),
            )
            self._conn.commit()

    def get_folder_id(self, local_rel_path: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT remote_id FROM folder_map WHERE local_rel_path=?", (local_rel_path,)
            ).fetchone()
        return None if row is None else str(row["remote_id"])

    def upsert_folder(self, *, local_rel_path: str, remote_path: str, remote_id: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO folder_map(local_rel_path, remote_path, remote_id, updated_at)
                VALUES(?,?,?,?)
                ON CONFLICT(local_rel_path) DO UPDATE SET
                    remote_path=excluded.remote_path,
                    remote_id=excluded.remote_id,
                    updated_at=excluded.updated_at
                """,
                (local_rel_path, remote_path, remote_id, time()),
            )
            self._conn.commit()

    # ----------------------------------------------------------- change tokens
    def get_change_token(self, account_key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT token FROM change_tokens WHERE account_key=?", (account_key,)
            ).fetchone()
        return None if row is None else str(row["token"])

    def set_change_token(self, account_key: str, token: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO change_tokens(account_key, token, updated_at) VALUES(?,?,?)
                ON CONFLICT(account_key) DO UPDATE SET token=excluded.token, updated_at=excluded.updated_at
                """,
                (account_key, token, time()),
            )
            self._conn.commit()

    # ------------------------------------------------------------- sync items
    def get_sync_item(self, pair_id: str, local_rel: str) -> SyncItem | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sync_items WHERE pair_id=? AND local_rel=?", (pair_id, local_rel)
            ).fetchone()
        return None if row is None else SyncItem(**dict(row))

    def list_sync_items(self, pair_id: str) -> list[SyncItem]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM sync_items WHERE pair_id=?", (pair_id,)
            ).fetchall()
        return [SyncItem(**dict(r)) for r in rows]

    def upsert_sync_item(
        self,
        *,
        pair_id: str,
        local_rel: str,
        remote_id: str,
        local_mtime_ns: int,
        local_size: int,
        sha256: str,
        remote_modified: str,
        remote_md5: str,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO sync_items(pair_id, local_rel, remote_id, local_mtime_ns, local_size,
                                       sha256, remote_modified, remote_md5, updated_at)
                VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(pair_id, local_rel) DO UPDATE SET
                    remote_id=excluded.remote_id,
                    local_mtime_ns=excluded.local_mtime_ns,
                    local_size=excluded.local_size,
                    sha256=excluded.sha256,
                    remote_modified=excluded.remote_modified,
                    remote_md5=excluded.remote_md5,
                    updated_at=excluded.updated_at
                """,
                (pair_id, local_rel, remote_id, local_mtime_ns, local_size, sha256,
                 remote_modified, remote_md5, time()),
            )
            self._conn.commit()

    def delete_sync_item(self, pair_id: str, local_rel: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM sync_items WHERE pair_id=? AND local_rel=?", (pair_id, local_rel)
            )
            self._conn.commit()

    # ------------------------------------------------------------ remote cache
    def cache_remote_children(self, parent_id: str, files: list) -> None:
        """Replace the cached child list of a remote folder. `files` are RemoteFile."""
        now = time()
        with self._lock:
            self._conn.execute("DELETE FROM remote_nodes WHERE parent_id=?", (parent_id,))
            self._conn.executemany(
                """
                INSERT OR REPLACE INTO remote_nodes(id, name, mime_type, size, modified_time,
                                                    parent_id, is_folder, fetched_at)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                [
                    (f.id, f.name, f.mime_type, f.size, f.modified_time, parent_id,
                     1 if getattr(f, "is_folder", False) else 0, now)
                    for f in files
                ],
            )
            self._conn.commit()

    def get_cached_children(self, parent_id: str) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM remote_nodes WHERE parent_id=? ORDER BY is_folder DESC, name",
                (parent_id,),
            ).fetchall()

    # --------------------------------------------------------------- transfers
    def add_transfer(self, *, kind: str, name: str, local_path: str = "", remote_id: str = "",
                     total_bytes: int = 0) -> int:
        now = time()
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO transfers(kind, name, local_path, remote_id, status, bytes_done,
                                      total_bytes, error, created_at, updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (kind, name, local_path, remote_id, "queued", 0, total_bytes, "", now, now),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def update_transfer(self, transfer_id: int, *, status: str | None = None,
                        bytes_done: int | None = None, total_bytes: int | None = None,
                        error: str | None = None) -> None:
        sets, params = [], []
        for col, val in (("status", status), ("bytes_done", bytes_done),
                         ("total_bytes", total_bytes), ("error", error)):
            if val is not None:
                sets.append(f"{col}=?")
                params.append(val)
        if not sets:
            return
        sets.append("updated_at=?")
        params.append(time())
        params.append(transfer_id)
        with self._lock:
            self._conn.execute(f"UPDATE transfers SET {', '.join(sets)} WHERE id=?", params)
            self._conn.commit()

    def list_transfers(self, limit: int = 100) -> list[TransferRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM transfers ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [TransferRecord(**dict(r)) for r in rows]

    # ------------------------------------------------------------------- stats
    def stats(self) -> dict[str, int]:
        with self._lock:
            file_count = self._conn.execute("SELECT COUNT(*) AS c FROM files").fetchone()["c"]
            folder_count = self._conn.execute("SELECT COUNT(*) AS c FROM folder_map").fetchone()["c"]
            transfer_count = self._conn.execute("SELECT COUNT(*) AS c FROM transfers").fetchone()["c"]
        return {"files": int(file_count), "folders": int(folder_count), "transfers": int(transfer_count)}
