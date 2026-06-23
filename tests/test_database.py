from pathlib import Path

from electridrive.storage.database import SyncDatabase


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
