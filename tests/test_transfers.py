import threading
from pathlib import Path

from electridrive.transfers import TransferManager, TransferState
from fakes import FakeDrive


def test_upload_folder_all_done(tmp_path: Path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.txt").write_text("aaa", encoding="utf-8")
    (root / "b.txt").write_text("bbbb", encoding="utf-8")

    fake = FakeDrive()
    mgr = TransferManager(fake, max_workers=2)
    ids = mgr.enqueue_upload(root, "root")
    assert mgr.wait_idle(timeout=10)
    mgr.shutdown()

    states = {t.id: t.state for t in mgr.snapshot()}
    assert all(states[i] == TransferState.DONE for i in ids)
    assert len(fake.uploads) == 2


def test_download_folder_and_export(tmp_path: Path):
    fake = FakeDrive()
    folder = fake.add_folder("Docs")
    fake.add_file("plain.bin", b"binary", parent=folder)
    fake.add_file("Memo", b"memo", parent=folder, mime="application/vnd.google-apps.document")

    remote = fake.get_metadata(folder)
    mgr = TransferManager(fake, max_workers=2)
    mgr.enqueue_download(remote, tmp_path)
    assert mgr.wait_idle(timeout=10)
    mgr.shutdown()

    assert (tmp_path / "Docs" / "plain.bin").read_bytes() == b"binary"
    assert (tmp_path / "Docs" / "Memo.docx").read_bytes() == b"EXPORTED:memo"
    assert all(t.state == TransferState.DONE for t in mgr.snapshot())


def test_cancel_running_transfer(tmp_path: Path):
    f = tmp_path / "big.txt"
    f.write_text("x", encoding="utf-8")

    class BlockingDrive(FakeDrive):
        def __init__(self):
            super().__init__()
            self.entered = threading.Event()

        def upload_file(self, local_file, parent_id, remote_name, progress_cb=None):
            self.entered.set()
            for _ in range(2000):  # spin, surfacing cancellation via progress_cb
                if progress_cb:
                    progress_cb(0, 100)
                threading.Event().wait(0.005)
            return "never"

    fake = BlockingDrive()
    mgr = TransferManager(fake, max_workers=1)
    ids = mgr.enqueue_upload(f, "root")
    assert fake.entered.wait(timeout=5)
    mgr.cancel(ids[0])
    assert mgr.wait_idle(timeout=5)
    mgr.shutdown()

    assert mgr.snapshot()[0].state == TransferState.CANCELED


def test_failure_isolation_and_retry(tmp_path: Path):
    class FailOnceDrive(FakeDrive):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        def download_file(self, file_id, dest_path, progress_cb=None):
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("boom")
            return super().download_file(file_id, dest_path, progress_cb)

    fake = FailOnceDrive()
    fid = fake.add_file("data.bin", b"payload")
    remote = fake.get_metadata(fid)

    mgr = TransferManager(fake, max_workers=1)
    ids = mgr.enqueue_download(remote, tmp_path)
    assert mgr.wait_idle(timeout=10)
    assert mgr.snapshot()[0].state == TransferState.FAILED

    new_id = mgr.retry(ids[0])
    assert new_id is not None
    assert mgr.wait_idle(timeout=10)
    mgr.shutdown()

    new_t = next(t for t in mgr.snapshot() if t.id == new_id)
    assert new_t.state == TransferState.DONE
    assert (tmp_path / "data.bin").read_bytes() == b"payload"
