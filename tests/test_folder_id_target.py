"""Regression gates for exact existing Google Drive folder-ID targets."""
import pytest

from electridrive.google_api.client import DRIVE_FOLDER_MIME, GoogleDriveClient
from electridrive.google_api.folder_target import (
    direct_folder_id, require_complete_folder_access,
)

ID = "1SyntheticDriveFolderId00000000"


class Response:
    def __init__(self, data):
        self.data = data

    def execute(self):
        return self.data


class FolderService:
    def __init__(self, data=None):
        self.data = data if data is not None else {
            "id": ID, "name": "Dokumente", "mimeType": DRIVE_FOLDER_MIME,
        }
        self.get_calls = []

    def files(self):
        return self

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        return Response(self.data)

    def list(self, **kwargs):
        pytest.fail("Direct Drive ID performed a folder name search")

    def create(self, **kwargs):
        pytest.fail("Direct Drive ID attempted folder creation")


def test_direct_folder_id_parser():
    assert direct_folder_id("id:" + ID) == ID
    assert direct_folder_id("Dokumente") is None
    assert direct_folder_id("qa:" + ID) is None
    assert direct_folder_id(ID) is None
    for bad in ("id:", "id:root", "id:../Dokumente", "id:abc/def",
                "id:https://drive.google.com", "id:" + ID + "?x=1",
                "id:" + ID + "/sub", "id:" + ID + " "):
        with pytest.raises(ValueError, match="Invalid Drive folder ID"):
            direct_folder_id(bad)


def test_exact_id_resolves_without_search_or_create():
    service = FolderService()
    result = GoogleDriveClient(service=service).ensure_folder_path("id:" + ID)
    assert result == ID
    assert len(service.get_calls) == 1
    assert service.get_calls[0]["fileId"] == ID


@pytest.mark.parametrize("metadata", [
    {"id": ID, "name": "file", "mimeType": "text/plain"},
    {"id": ID, "name": "Dokumente", "mimeType": DRIVE_FOLDER_MIME, "trashed": True},
    {"id": "wrong", "name": "Dokumente", "mimeType": DRIVE_FOLDER_MIME},
])
def test_exact_id_refuses_bad_metadata_without_fallback(metadata):
    service = FolderService(metadata)
    with pytest.raises(ValueError, match="Drive folder ID"):
        GoogleDriveClient(service=service).ensure_folder_path("id:" + ID)
    assert len(service.get_calls) == 1


def test_bad_direct_id_never_issues_a_drive_request():
    service = FolderService()
    with pytest.raises(ValueError, match="Invalid Drive folder ID"):
        GoogleDriveClient(service=service).ensure_folder_path("id:root")
    assert service.get_calls == []


def test_existing_id_auto_sync_requires_full_drive_scope(monkeypatch):
    from electridrive.config import DEFAULT_SCOPE, FULL_DRIVE_SCOPE
    monkeypatch.setattr("electridrive.config.selected_scopes", lambda: [DEFAULT_SCOPE])
    with pytest.raises(PermissionError, match="drive.file"):
        require_complete_folder_access("id:" + ID)
    require_complete_folder_access("Dokumente")
    require_complete_folder_access("qa:" + ID)
    monkeypatch.setattr("electridrive.config.selected_scopes", lambda: [FULL_DRIVE_SCOPE])
    require_complete_folder_access("id:" + ID)


def test_upload_only_id_is_blocked_before_cloud_write(tmp_path, monkeypatch):
    from electridrive.config import DEFAULT_SCOPE
    from electridrive.storage.database import SyncDatabase
    from electridrive.sync.engine import UploadOnlySyncEngine
    from fakes import FakeDrive

    monkeypatch.setattr("electridrive.config.selected_scopes", lambda: [DEFAULT_SCOPE])
    local = tmp_path / "local"
    local.mkdir()
    (local / "synthetic.txt").write_text("synthetic", encoding="utf-8")
    fake = FakeDrive()
    db = SyncDatabase(tmp_path / "sync.sqlite")
    try:
        engine = UploadOnlySyncEngine(drive_client=fake, database=db)
        with pytest.raises(PermissionError, match="drive.file"):
            engine.sync_up(local, "id:" + ID)
        assert fake.uploads == [] and fake.updates == []
        assert set(fake.nodes) == {"root"}
    finally:
        db.close()


def test_two_way_can_bind_existing_id_in_down_only_mode(tmp_path, monkeypatch):
    from electridrive.config import FULL_DRIVE_SCOPE, SyncPair
    from electridrive.storage.database import SyncDatabase
    from electridrive.sync.twoway import TwoWaySyncEngine
    from fakes import FakeDrive

    monkeypatch.setattr("electridrive.config.selected_scopes", lambda: [FULL_DRIVE_SCOPE])

    class ExactDrive(FakeDrive):
        def ensure_folder_path(self, path):
            assert path == "id:" + ID
            assert self.get_metadata(ID).is_folder
            return ID

        def find_folder(self, *args, **kwargs):
            pytest.fail("Exact ID attempted path lookup")

        def create_folder(self, *args, **kwargs):
            pytest.fail("Exact ID attempted folder creation")

    fake = ExactDrive()
    old = fake.add_folder("Dokumente")
    node = fake.nodes.pop(old)
    node["id"] = ID
    fake.nodes[ID] = node
    local = tmp_path / "local"
    local.mkdir()
    db = SyncDatabase(tmp_path / "sync.sqlite")
    try:
        pair = SyncPair(local_path=str(local), remote_folder="id:" + ID,
                        direction="down_only", delete_policy="off")
        engine = TwoWaySyncEngine(fake, db, pair)
        assert engine._bind_roots() == ID
        assert engine._bound_root_id == ID
    finally:
        db.close()


def test_two_way_rejects_incomplete_visibility_before_root_binding(tmp_path, monkeypatch):
    from electridrive.config import DEFAULT_SCOPE, SyncPair
    from electridrive.storage.database import SyncDatabase
    from electridrive.sync.twoway import TwoWaySyncEngine
    from fakes import FakeDrive

    monkeypatch.setattr("electridrive.config.selected_scopes", lambda: [DEFAULT_SCOPE])
    local = tmp_path / "local"
    local.mkdir()
    fake = FakeDrive()
    db = SyncDatabase(tmp_path / "sync.sqlite")
    try:
        pair = SyncPair(local_path=str(local), remote_folder="id:" + ID,
                        direction="two_way", delete_policy="off")
        engine = TwoWaySyncEngine(fake, db, pair)
        with pytest.raises(PermissionError, match="drive.file"):
            engine._bind_roots()
        assert fake.uploads == [] and fake.updates == []
        assert set(fake.nodes) == {"root"}
    finally:
        db.close()


def test_saved_binding_cannot_redirect_exact_folder_id(tmp_path, monkeypatch):
    import json
    from electridrive.config import FULL_DRIVE_SCOPE, SyncPair
    from electridrive.storage.database import SyncDatabase
    from electridrive.sync.twoway import TwoWaySyncEngine
    from fakes import FakeDrive

    monkeypatch.setattr("electridrive.config.selected_scopes", lambda: [FULL_DRIVE_SCOPE])
    local = tmp_path / "local"
    local.mkdir()
    fake = FakeDrive()
    db = SyncDatabase(tmp_path / "sync.sqlite")
    try:
        pair = SyncPair(local_path=str(local), remote_folder="id:" + ID,
                        direction="down_only", delete_policy="off")
        engine = TwoWaySyncEngine(fake, db, pair)
        identity = [local.stat().st_dev, local.stat().st_ino]
        db.set_meta(engine._binding_key, json.dumps({
            "local_identity": identity, "remote_id": "wrong-remote-id",
        }))
        with pytest.raises(ValueError, match="exact Drive folder ID"):
            engine._bind_roots()
        assert fake.uploads == [] and fake.updates == []
    finally:
        db.close()
