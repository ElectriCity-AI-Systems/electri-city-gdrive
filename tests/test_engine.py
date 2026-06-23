from pathlib import Path

from electridrive.storage.database import SyncDatabase
from electridrive.sync.engine import UploadOnlySyncEngine


class FakeDrive:
    def __init__(self):
        self.uploads = []
        self.folders = {}

    def ensure_folder_path(self, remote_path: str) -> str:
        self.folders.setdefault(remote_path, f'folder-{len(self.folders)+1}')
        return self.folders[remote_path]

    def upload_file(self, local_file: Path, parent_id: str, remote_name: str) -> str:
        remote_id = f'file-{len(self.uploads)+1}'
        self.uploads.append((local_file, parent_id, remote_name, remote_id))
        return remote_id

    def list_files(self, limit: int = 20):
        return []


def test_upload_only_sync_uploads_once_then_skips(tmp_path: Path):
    local = tmp_path / 'local'
    local.mkdir()
    (local / 'a.txt').write_text('hello', encoding='utf-8')
    (local / '.secret').write_text('skip', encoding='utf-8')
    (local / 'node_modules').mkdir()
    (local / 'node_modules' / 'x.js').write_text('skip', encoding='utf-8')

    db = SyncDatabase(tmp_path / 'state.sqlite3')
    fake = FakeDrive()
    try:
        engine = UploadOnlySyncEngine(drive_client=fake, database=db)
        first = engine.sync_up(local, 'ElectriDrive/Test')
        assert first.uploaded == 1
        assert first.skipped == 0
        assert len(fake.uploads) == 1

        second = engine.sync_up(local, 'ElectriDrive/Test')
        assert second.uploaded == 0
        assert second.skipped == 1
        assert len(fake.uploads) == 1
    finally:
        db.close()
