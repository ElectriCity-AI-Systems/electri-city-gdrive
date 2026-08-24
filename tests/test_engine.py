from pathlib import Path

from electridrive.storage.database import SyncDatabase
from electridrive.sync.engine import UploadOnlySyncEngine


class FakeDrive:
    def __init__(self):
        self.uploads = []
        self.updates = []
        self.folders = {}
        self.files = {}

    def ensure_folder_path(self, remote_path: str) -> str:
        self.folders.setdefault(remote_path, f'folder-{len(self.folders)+1}')
        return self.folders[remote_path]

    def upload_file(self, local_file: Path, parent_id: str, remote_name: str) -> str:
        remote_id = f'file-{len(self.uploads)+1}'
        self.uploads.append((local_file, parent_id, remote_name, remote_id))
        self.files[remote_id] = Path(local_file).read_bytes()
        return remote_id

    def update_file(
        self, remote_id: str, local_file: Path, remote_name: str | None = None
    ) -> str:
        self.updates.append((remote_id, local_file, remote_name))
        self.files[remote_id] = Path(local_file).read_bytes()
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


def test_upload_only_changed_file_updates_existing_remote_id(tmp_path: Path):
    local = tmp_path / "local"
    local.mkdir()
    source = local / "a.txt"
    source.write_text("first", encoding="utf-8")

    db = SyncDatabase(tmp_path / "state.sqlite3")
    fake = FakeDrive()
    try:
        engine = UploadOnlySyncEngine(drive_client=fake, database=db)
        first = engine.sync_up(local, "ElectriDrive/Test")
        original_id = db.get_file(str(source)).remote_id

        source.write_text("second", encoding="utf-8")
        second = engine.sync_up(local, "ElectriDrive/Test")
        updated = db.get_file(str(source))

        assert first.uploaded == 1
        assert second.uploaded == 1
        assert len(fake.uploads) == 1
        assert len(fake.updates) == 1
        assert fake.updates[0][0] == original_id
        assert updated is not None and updated.remote_id == original_id
        assert fake.files == {original_id: b"second"}
    finally:
        db.close()
