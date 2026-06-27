import sqlite3
from pathlib import Path

from electridrive.google_api.client import RemoteFile
from electridrive.storage.database import CACHE_SCHEMA_VERSION, SyncDatabase


def test_file_upsert_and_read(tmp_path: Path):
    db = SyncDatabase(tmp_path / 'state.sqlite3')
    try:
        db.upsert_file(
            local_path='/a/b.txt',
            remote_id='remote123',
            remote_path='ElectriDrive/b.txt',
            size=12,
            mtime_ns=1234,
            sha256='abc',
            status='uploaded',
        )
        record = db.get_file('/a/b.txt')
        assert record is not None
        assert record.remote_id == 'remote123'
        assert record.size == 12
    finally:
        db.close()


def test_folder_map(tmp_path: Path):
    db = SyncDatabase(tmp_path / 'state.sqlite3')
    try:
        db.upsert_folder(local_rel_path='sub', remote_path='ElectriDrive/sub', remote_id='folder123')
        assert db.get_folder_id('sub') == 'folder123'
    finally:
        db.close()


def test_remote_nodes_cache_migration_rebuilds_with_md5(tmp_path: Path):
    db_path = tmp_path / 'state.sqlite3'
    # Simulate a pre-migration DB: remote_nodes WITHOUT md5/trashed, user_version 0.
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        CREATE TABLE remote_nodes (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, mime_type TEXT, size INTEGER,
            modified_time TEXT, parent_id TEXT, is_folder INTEGER NOT NULL DEFAULT 0,
            fetched_at REAL NOT NULL
        );
        INSERT INTO remote_nodes(id, name, parent_id, is_folder, fetched_at)
        VALUES ('x', 'old', 'root', 0, 0);
        """
    )
    con.commit()
    con.close()

    db = SyncDatabase(db_path)
    try:
        cols = {r[1] for r in db._conn.execute('PRAGMA table_info(remote_nodes)').fetchall()}
        assert 'md5' in cols and 'trashed' in cols
        # the md5-less row was dropped by the rebuild (no stale rows survive)
        assert db.get_cached_children('root') == []
        assert db._conn.execute('PRAGMA user_version').fetchone()[0] == CACHE_SCHEMA_VERSION
    finally:
        db.close()


def test_cache_remote_children_persists_md5_and_trashed(tmp_path: Path):
    db = SyncDatabase(tmp_path / 'state.sqlite3')
    try:
        f = RemoteFile(id='f1', name='a.txt', mime_type='text/plain', size=3,
                       modified_time='2024-01-01T00:00:00Z', parents=('root',),
                       md5_checksum='abc123')
        db.cache_remote_children('root', [f])
        rows = db.get_cached_children('root')
        assert len(rows) == 1
        assert rows[0]['md5'] == 'abc123'
        assert rows[0]['trashed'] == 0
    finally:
        db.close()
