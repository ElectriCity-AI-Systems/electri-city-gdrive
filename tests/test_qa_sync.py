"""Offline regression coverage of QA engine and local I/O boundaries."""
from pathlib import Path
import pytest
from electridrive import qa_policy as qa
from electridrive.config import SyncPair
from electridrive.sync.tree import canonical_sync_root, scan_local_tree
from electridrive.sync.hasher import sha256_file
from electridrive.sync.twoway import TwoWaySyncEngine
from electridrive.sync.engine import UploadOnlySyncEngine

@pytest.fixture
def sealed(monkeypatch, tmp_path):
    monkeypatch.setattr(qa, "_active", None)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    policy = qa.initialize(tmp_path / "profile", tmp_path / "synthetic")
    monkeypatch.setenv("ELECTRIDRIVE_QA_PROFILE", str(policy.profile))
    return qa.current_policy()

class Untouched:
    def __getattr__(self, name):
        raise AssertionError("unexpected side effect: " + name)

@pytest.mark.parametrize("path", ["relative", "../synthetic"])
def test_raw_relative_root_rejected_before_resolution(sealed, path):
    with pytest.raises(qa.QaSafetyError):
        canonical_sync_root(Path(path), create=True)

def test_hash_outside_root_is_denied(sealed, tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("synthetic outside fixture")
    with pytest.raises(qa.QaSafetyError):
        sha256_file(outside)

def test_engine_rejects_invalid_pair_before_database_or_client(sealed):
    pair = SyncPair(str(sealed.local_root), "Personal", delete_policy="off")
    with pytest.raises(qa.QaSafetyError):
        TwoWaySyncEngine(Untouched(), Untouched(), pair).run()

def test_legacy_engine_rejects_unregistered_target(sealed):
    with pytest.raises(qa.QaSafetyError):
        UploadOnlySyncEngine(drive_client=Untouched(), database=Untouched()).sync_up(sealed.local_root, "Personal")

def test_secure_scandir_preserves_absolute_descendant_paths(sealed):
    sub = sealed.local_root / "sub"
    sub.mkdir()
    (sub / "file").write_text("synthetic")
    tree = scan_local_tree(sealed.local_root)
    assert tree.files == (sub / "file",)
    assert not tree.errors

def test_download_destination_is_denied_before_remote_or_mkdir(sealed, tmp_path):
    from electridrive.sync.downloader import plan_download
    destination = tmp_path / "outside" / "new"
    with pytest.raises(qa.QaSafetyError):
        plan_download(Untouched(), Untouched(), destination)
    assert not destination.exists()

def test_upload_source_is_denied_before_remote(sealed, tmp_path):
    from electridrive.sync.uploader import plan_upload
    outside = tmp_path / "outside"
    outside.write_text("synthetic")
    with pytest.raises(qa.QaSafetyError):
        plan_upload(Untouched(), outside, "root")

def test_fuse_tree_is_denied_before_cache_creation(sealed, tmp_path):
    from electridrive.vfs.fuse_mount import DriveTree
    cache = tmp_path / "uncreated-cache"
    with pytest.raises(qa.QaSafetyError):
        DriveTree(Untouched(), "root", cache)
    assert not cache.exists()

def test_worker_revalidates_queued_source_before_any_cloud_access(sealed, tmp_path):
    from electridrive.transfers.manager import TransferManager, TransferKind, TransferState
    from fakes import FakeDrive
    manager = TransferManager(FakeDrive())
    outside = tmp_path / "outside"
    outside.write_text("synthetic")
    transfer = manager._new_transfer(TransferKind.UPLOAD, "file", local_path=outside)
    manager._outstanding = 1
    manager._run(transfer)
    assert transfer.state == TransferState.FAILED
    assert "outside" in transfer.error
    with pytest.raises(qa.QaSafetyError):
        manager.retry(transfer.id)
