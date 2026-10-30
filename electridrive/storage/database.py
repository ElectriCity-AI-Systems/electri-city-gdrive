from __future__ import annotations

import sqlite3
import stat
import fcntl
import hashlib
import json
import math
import os
from contextlib import contextmanager
from electridrive import qa_policy as qa
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
class SyncFolder:
    """A synchronized directory in one configured pair."""

    pair_id: str
    local_rel: str
    remote_id: str
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
    _TABLES = ("files", "folder_map", "app_meta", "change_tokens", "sync_items",
               "sync_folders", "remote_nodes", "transfers", "sqlite_sequence")

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._qa = qa.current_policy()
        self._lock = threading.RLock()
        self._qa_lock_fd = None
        self._initializing = True
        if self._qa:
            self._state_dir = self._qa.profile / "state/electridrive"
            self._snapshot = self._state_dir / "qa-sync-state.json"
            self._lock_path = self._state_dir / "qa-sync-state.lock"
            self._snapshot_identity = None
            self._snapshot_digest = None
            self._open_qa_lock()
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # QA never opens a SQLite pathname, including a pre-existing database.
            self._conn = sqlite3.connect(":memory:" if self._qa else self.db_path,
                                         check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._init_schema()
            if self._qa:
                self._restore_snapshot()
            self._initializing = False
            if self._qa:
                with self._access():
                    self._commit()
        except Exception:
            if hasattr(self, "_conn"):
                self._conn.close()
            self._release_qa_lock()
            raise

    def _open_qa_lock(self):
        qa.check_profile_file(self._lock_path, strict=True)
        qa._no_links(self._state_dir)
        parent = qa._open_directory(self._state_dir)
        try:
            self._state_identity = self._identity(os.fstat(parent))
            fd = os.open(self._lock_path.name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                         0o600, dir_fd=parent)
            self._qa_lock_fd = fd
            opened = os.fstat(fd)
            if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                    or opened.st_uid != os.getuid() or opened.st_mode & 0o077):
                raise qa.QaSafetyError("QA safety stop: unsafe database lock")
            qa.check_profile_file(self._lock_path, strict=True)
            self._lock_identity = self._identity(os.fstat(fd))
            if self._lock_identity != self._identity(self._lock_path.lstat()):
                raise qa.QaSafetyError("QA safety stop: database lock replaced")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise qa.QaSafetyError("QA safety stop: database already in use") from exc
        except Exception:
            self._release_qa_lock()
            raise
        finally:
            os.close(parent)

    @staticmethod
    def _identity(st):
        return st.st_dev, st.st_ino

    def _release_qa_lock(self):
        if self._qa_lock_fd is not None:
            os.close(self._qa_lock_fd)
            self._qa_lock_fd = None

    def _verify_qa(self):
        if not self._qa:
            if qa.current_policy():
                raise qa.QaSafetyError("QA safety stop: ordinary database cannot be reused")
            return
        if qa.current_policy() is not self._qa:
            raise qa.QaSafetyError("QA safety stop: database profile changed")
        qa._no_links(self._state_dir)
        qa.check_profile_file(self._lock_path, strict=True)
        if (self._qa_lock_fd is None
                or self._identity(self._state_dir.stat()) != self._state_identity
                or self._identity(self._lock_path.lstat()) != self._lock_identity
                or self._identity(os.fstat(self._qa_lock_fd)) != self._lock_identity):
            raise qa.QaSafetyError("QA safety stop: database directory or lock changed")
        qa.check_profile_file(self._snapshot, strict=True)
        if not self._initializing:
            if self._snapshot_identity is None:
                if self._snapshot.exists():
                    raise qa.QaSafetyError("QA safety stop: unexpected database snapshot")
            elif (not self._snapshot.exists()
                    or self._identity(self._snapshot.lstat()) != self._snapshot_identity
                    or hashlib.sha256(qa._read(self._snapshot)).hexdigest() != self._snapshot_digest):
                raise qa.QaSafetyError("QA safety stop: database snapshot changed")

    @contextmanager
    def _access(self):
        with self._lock:
            self._verify_qa()
            yield

    def _schema(self):
        return {table: self._conn.execute("PRAGMA table_info(" + table + ")").fetchall()
                for table in self._TABLES}

    def _restore_snapshot(self):
        qa.check_profile_file(self._snapshot, strict=True)
        if not self._snapshot.exists():
            return
        try:
            raw = qa._read(self._snapshot)
            data = json.loads(raw)
            schema = self._schema()
            if (not isinstance(data, dict) or set(data) != {"version", "tables"}
                    or type(data["version"]) is not int or data["version"] != 1
                    or not isinstance(data["tables"], dict)
                    or set(data["tables"]) != set(self._TABLES)):
                raise ValueError("invalid schema")
            for table in self._TABLES:
                rows = data["tables"][table]
                columns = schema[table]
                if not isinstance(rows, list):
                    raise ValueError("invalid rows")
                for row in rows:
                    if not isinstance(row, list) or len(row) != len(columns):
                        raise ValueError("invalid row shape")
                    for value, column in zip(row, columns):
                        if (value is not None and type(value) not in (str, int, float)
                                or isinstance(value, float) and not math.isfinite(value)
                                or isinstance(value, int) and not -(2**63) <= value < 2**63
                                or value is None and column[3]):
                            raise ValueError("invalid SQL value")
                # Names originate only in the fixed application schema above.
                if table == "sqlite_sequence":
                    self._conn.execute("DELETE FROM sqlite_sequence")
                self._conn.executemany("INSERT INTO " + table + " VALUES (" +
                                       ",".join("?" for _ in columns) + ")", rows)
            self._conn.commit()
            self._snapshot_identity = self._identity(self._snapshot.lstat())
            self._snapshot_digest = hashlib.sha256(raw).hexdigest()
        except (ValueError, TypeError, KeyError, sqlite3.Error, OverflowError) as exc:
            raise qa.QaSafetyError("QA safety stop: malformed database snapshot") from exc

    def _commit(self):
        self._conn.commit()
        if self._qa and not self._initializing:
            self._verify_qa()
            data = {"version": 1, "tables": {
                table: [list(row) for row in self._conn.execute("SELECT * FROM " + table)]
                for table in self._TABLES}}
            raw = qa._write(self._snapshot, data)
            self._snapshot_identity = self._identity(self._snapshot.lstat())
            self._snapshot_digest = hashlib.sha256(raw).hexdigest()

    def close(self) -> None:
        with self._lock:
            try:
                self._verify_qa()
            finally:
                self._conn.close()
                self._release_qa_lock()

    def _init_schema(self) -> None:
        with self._access():
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

                CREATE TABLE IF NOT EXISTS sync_folders (
                    pair_id TEXT NOT NULL,
                    local_rel TEXT NOT NULL,
                    remote_id TEXT NOT NULL,
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
            self._commit()

    # --------------------------------------------------------------- files index
    def get_file(self, local_path: str) -> FileRecord | None:
        with self._access():
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
        with self._access():
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
            self._commit()

    def get_folder_id(self, local_rel_path: str) -> str | None:
        with self._access():
            row = self._conn.execute(
                "SELECT remote_id FROM folder_map WHERE local_rel_path=?", (local_rel_path,)
            ).fetchone()
        return None if row is None else str(row["remote_id"])

    def upsert_folder(self, *, local_rel_path: str, remote_path: str, remote_id: str) -> None:
        with self._access():
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
            self._commit()

    # ----------------------------------------------------------- change tokens
    def get_change_token(self, account_key: str) -> str | None:
        with self._access():
            row = self._conn.execute(
                "SELECT token FROM change_tokens WHERE account_key=?", (account_key,)
            ).fetchone()
        return None if row is None else str(row["token"])

    def set_change_token(self, account_key: str, token: str) -> None:
        with self._access():
            self._conn.execute(
                """
                INSERT INTO change_tokens(account_key, token, updated_at) VALUES(?,?,?)
                ON CONFLICT(account_key) DO UPDATE SET token=excluded.token, updated_at=excluded.updated_at
                """,
                (account_key, token, time()),
            )
            self._commit()

    # ------------------------------------------------------------- sync items
    def get_sync_item(self, pair_id: str, local_rel: str) -> SyncItem | None:
        with self._access():
            row = self._conn.execute(
                "SELECT * FROM sync_items WHERE pair_id=? AND local_rel=?", (pair_id, local_rel)
            ).fetchone()
        return None if row is None else SyncItem(**dict(row))

    def list_sync_items(self, pair_id: str) -> list[SyncItem]:
        with self._access():
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
        with self._access():
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
            self._commit()

    def delete_sync_item(self, pair_id: str, local_rel: str) -> None:
        with self._access():
            self._conn.execute(
                "DELETE FROM sync_items WHERE pair_id=? AND local_rel=?", (pair_id, local_rel)
            )
            self._commit()

    # ----------------------------------------------------------- sync folders
    def list_sync_folders(self, pair_id: str) -> list[SyncFolder]:
        with self._access():
            rows = self._conn.execute(
                "SELECT * FROM sync_folders WHERE pair_id=? ORDER BY local_rel",
                (pair_id,),
            ).fetchall()
        return [SyncFolder(**dict(row)) for row in rows]

    def replace_sync_folders(self, pair_id: str, folders: dict[str, str]) -> None:
        """Atomically store the complete current directory mapping for a pair."""
        now = time()
        with self._access():
            self._conn.execute("DELETE FROM sync_folders WHERE pair_id=?", (pair_id,))
            self._conn.executemany(
                """
                INSERT INTO sync_folders(pair_id, local_rel, remote_id, updated_at)
                VALUES(?,?,?,?)
                """,
                [
                    (pair_id, local_rel, remote_id, now)
                    for local_rel, remote_id in sorted(folders.items())
                ],
            )
            self._commit()

    # ------------------------------------------------------------ remote cache
    def cache_remote_children(self, parent_id: str, files: list) -> None:
        """Replace the cached child list of a remote folder. `files` are RemoteFile."""
        now = time()
        with self._access():
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
            self._commit()

    def get_cached_children(self, parent_id: str) -> list[sqlite3.Row]:
        with self._access():
            return self._conn.execute(
                "SELECT * FROM remote_nodes WHERE parent_id=? ORDER BY is_folder DESC, name",
                (parent_id,),
            ).fetchall()

    # --------------------------------------------------------------- transfers
    def add_transfer(self, *, kind: str, name: str, local_path: str = "", remote_id: str = "",
                     total_bytes: int = 0) -> int:
        now = time()
        with self._access():
            cur = self._conn.execute(
                """
                INSERT INTO transfers(kind, name, local_path, remote_id, status, bytes_done,
                                      total_bytes, error, created_at, updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (kind, name, local_path, remote_id, "queued", 0, total_bytes, "", now, now),
            )
            self._commit()
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
        with self._access():
            self._conn.execute(f"UPDATE transfers SET {', '.join(sets)} WHERE id=?", params)
            self._commit()

    def list_transfers(self, limit: int = 100) -> list[TransferRecord]:
        with self._access():
            rows = self._conn.execute(
                "SELECT * FROM transfers ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [TransferRecord(**dict(r)) for r in rows]

    # ------------------------------------------------------------------- stats
    def stats(self) -> dict[str, int]:
        with self._access():
            file_count = self._conn.execute("SELECT COUNT(*) AS c FROM files").fetchone()["c"]
            folder_count = self._conn.execute("SELECT COUNT(*) AS c FROM folder_map").fetchone()["c"]
            transfer_count = self._conn.execute("SELECT COUNT(*) AS c FROM transfers").fetchone()["c"]
        return {"files": int(file_count), "folders": int(folder_count), "transfers": int(transfer_count)}
