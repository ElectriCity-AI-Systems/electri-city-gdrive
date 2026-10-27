"""Offline completeness tests for default upload-only full backup."""
from __future__ import annotations

from pathlib import Path

from electridrive.sync.engine import UploadOnlySyncEngine
from electridrive.storage.database import SyncDatabase
from fakes import FakeDrive


def test_cli_upload_engine_defaults_to_all_regular_files(tmp_path: Path):
    root = tmp_path / "local"
    root.mkdir()
    wanted = {
        ".env": b"fixture-only",
        ".git/config": b"git-fixture",
        ".venv/secret.tmp": b"tmp-fixture",
        "node_modules/index.js": b"lib-fixture",
        "normal.txt": b"hello",
    }
    for rel, content in wanted.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    db = SyncDatabase(tmp_path / "state.sqlite3")
    fake = FakeDrive()
    try:
        result = UploadOnlySyncEngine(drive_client=fake, database=db).sync_up(
            root, "ElectriDrive/All-Backup-Tests"
        )
        assert result.failed == 0
        assert result.excluded == 0
        assert result.unexplained_omissions == 0
        assert result.uploaded == len(wanted)
        assert len(fake.uploads) == len(wanted)
        assert not fake.updates
    finally:
        db.close()


def test_cli_upload_engine_refuses_unsupported_link_without_mutations(
    tmp_path: Path,
):
    root = tmp_path / "local"
    root.mkdir()
    (root / "regular.txt").write_bytes(b"test")
    (root / "link").symlink_to(root / "regular.txt")
    db = SyncDatabase(tmp_path / "state.sqlite3")
    fake = FakeDrive()
    try:
        report = UploadOnlySyncEngine(drive_client=fake, database=db).sync_up(
            root, "ElectriDrive/No-Partial-Upload"
        )
        assert report.failed
        assert any("unsupported symlink" in e for e in report.errors)
        assert len(fake.nodes) == 1
        assert not fake.uploads
        assert not fake.updates
    finally:
        db.close()



def test_existing_saved_upload_pair_inherits_unfiltered_backup_mode():
    from electridrive.config import Settings
    legacy_settings = Settings.from_dict({
        "sync_pairs": [
            {"local_path": "/home/example/Musik",
             "remote_folder": "ElectriDrive/Musik",
             "direction": "up_only", "enabled": False},
            {"local_path": "/home/example/Brain",
             "remote_folder": "ElectriDrive/Brain",
             "direction": "up_only", "enabled": False},
        ]
    })
    assert len(legacy_settings.sync_pairs) == 2
    assert all(p.include_all_files for p in legacy_settings.sync_pairs)
    assert not any(p.enabled for p in legacy_settings.sync_pairs)
    assert all(p.include_all_files for p in
               Settings.from_dict(legacy_settings.to_dict()).sync_pairs)
