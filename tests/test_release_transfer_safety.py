from pathlib import Path
import threading

import pytest

from electridrive.transfers import TransferManager
from fakes import FakeDrive


def test_separate_download_requests_never_overwrite_each_other(tmp_path):
    drive = FakeDrive()
    a, b = drive.add_file('same', b'first'), drive.add_file('same', b'second')
    entered, release = threading.Event(), threading.Event()
    download = drive.download_file
    def blocked(*args, **kwargs):
        entered.set()
        release.wait(2)
        return download(*args, **kwargs)
    drive.download_file = blocked
    manager = TransferManager(drive, max_workers=1)
    try:
        ids = manager.enqueue_download(drive.get_metadata(a), tmp_path)
        assert entered.wait(1)
        ids += manager.enqueue_download(drive.get_metadata(b), tmp_path)
        release.set()
        assert manager.wait_idle(3)
        paths = [manager.get(i).dest_path for i in ids]
        assert paths[0] != paths[1]
        assert {p.read_bytes() for p in paths} == {b'first', b'second'}
    finally:
        release.set()
        manager.shutdown()


def test_shutdown_rejects_future_enqueues(tmp_path):
    drive = FakeDrive()
    manager = TransferManager(drive)
    manager.shutdown()
    path = tmp_path / 'a'
    path.write_bytes(b'a')
    with pytest.raises(RuntimeError, match='closed'):
        manager.enqueue_upload(path)


def test_download_refuses_file_created_after_planning(tmp_path):
    drive = FakeDrive()
    fid = drive.add_file('same', b'remote')
    original = drive.download_file
    def race(file_id, dest, progress_cb=None):
        (tmp_path / 'same').write_bytes(b'local-created-meanwhile')
        return original(file_id, dest, progress_cb)
    drive.download_file = race
    manager = TransferManager(drive, max_workers=1)
    try:
        ids = manager.enqueue_download(drive.get_metadata(fid), tmp_path)
        assert manager.wait_idle(2)
        assert (tmp_path / 'same').read_bytes() == b'local-created-meanwhile'
        assert manager.get(ids[0]).state.value == 'failed'
        assert not list(tmp_path.glob('.electridrive-*'))
    finally:
        manager.shutdown()


@pytest.mark.parametrize('swap', ['file', 'directory'])
def test_queued_upload_refuses_source_replaced_by_symlink(tmp_path, swap):
    selected = tmp_path / 'selected'
    selected.mkdir()
    source = selected / 'file.txt'
    source.write_bytes(b'selected-content')
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'file.txt').write_bytes(b'private-content')
    blocker = tmp_path / 'blocker'
    blocker.write_bytes(b'blocker')
    entered, release = threading.Event(), threading.Event()
    drive = FakeDrive()
    original = drive.upload_file
    def blocked(path, parent, name, progress_cb=None):
        if name == 'blocker':
            entered.set()
            release.wait(2)
        return original(path, parent, name, progress_cb)
    drive.upload_file = blocked
    manager = TransferManager(drive, max_workers=1)
    try:
        manager.enqueue_upload(blocker)
        assert entered.wait(1)
        task_id = manager.enqueue_upload(source)[0]
        if swap == 'file':
            source.unlink()
            source.symlink_to(outside / 'file.txt')
        else:
            selected.rename(tmp_path / 'selected-original')
            selected.symlink_to(outside, target_is_directory=True)
        release.set()
        assert manager.wait_idle(2)
        assert manager.get(task_id).state.value == 'failed'
        assert all(node['content'] != b'private-content' for node in drive.nodes.values())
        assert [upload[2] for upload in drive.uploads] == ['blocker']
    finally:
        release.set()
        manager.shutdown()


def test_refresh_retains_queue_and_rejects_other_account(tmp_path):
    first = FakeDrive()
    first.account_key = 'a'
    second = FakeDrive()
    second.account_key = 'a'
    other = FakeDrive()
    other.account_key = 'b'
    manager = TransferManager(first, account_key='a')
    try:
        manager.replace_client(second)
        with pytest.raises(ValueError, match='account'):
            manager.replace_client(other)
        path = tmp_path / 'file'
        path.write_bytes(b'content')
        tid = manager.enqueue_upload(path)[0]
        assert manager.wait_idle(2)
        assert manager.get(tid).state.value == 'done'
        assert len(second.uploads) == 1 and not first.uploads and not other.uploads
    finally:
        manager.shutdown()


from test_qa_integration import synthetic


def test_qa_explicit_journal_rejected_without_disk_effect(synthetic):
    from electridrive import qa_policy as qa
    from electridrive.transfers.journal import TransferJournal
    policy, drive, rid, remote = synthetic
    path = policy.profile / 'state/electridrive/nonexistent/queue.sqlite3'
    for constructor in (
        lambda: TransferManager(drive, journal_path=path, account_key='a'),
        lambda: TransferJournal(path, 'a'),
    ):
        with pytest.raises(qa.QaSafetyError):
            constructor()
        assert not path.parent.exists()


def test_qa_upload_uses_allowed_path_and_download_checks_replaced_parent(synthetic):
    from electridrive import qa_policy as qa
    policy, drive, rid, remote = synthetic
    source = policy.local_root / 'source.txt'
    source.write_bytes(b'QA content')
    manager = TransferManager(drive, max_workers=1)
    try:
        tid = manager.enqueue_upload(source, rid)[0]
        assert manager.wait_idle(2)
        assert manager.get(tid).state.value == 'done'
        dest = policy.local_root / 'dest'
        dest.mkdir()
        fid = drive.add_file('data', b'cloud', parent=rid)
        original = drive.download_file
        def race(file_id, partial, progress_cb=None):
            result = original(file_id, partial, progress_cb)
            dest.rename(policy.local_root / 'old-dest')
            dest.mkdir()
            return result
        drive.download_file = race
        manager._client.list_folder(rid)
        tid = manager.enqueue_download(manager._client.get_metadata(fid), dest)[0]
        assert manager.wait_idle(2)
        assert manager.get(tid).state.value == 'failed'
        assert not (dest / 'data').exists()
        assert not (policy.local_root / 'old-dest/data').exists()
    finally:
        manager.shutdown()


def test_running_upload_keeps_pinned_source_and_client_after_refresh(tmp_path):
    first, refreshed = FakeDrive(), FakeDrive()
    first.account_key = refreshed.account_key = 'same-account'
    source = tmp_path / 'file.txt'
    source.write_bytes(b'selected')
    entered, release = threading.Event(), threading.Event()
    original = first.upload_file
    def replace_after_open(path, parent, name, progress_cb=None):
        entered.set()
        release.wait(2)
        source.unlink()
        source.write_bytes(b'replacement')
        return original(path, parent, name, progress_cb)
    first.upload_file = replace_after_open
    manager = TransferManager(first, max_workers=1)
    try:
        tid = manager.enqueue_upload(source)[0]
        assert entered.wait(1)
        manager.replace_client(refreshed)
        release.set()
        assert manager.wait_idle(2)
        assert manager.get(tid).state.value == 'done'
        assert first.nodes[first.uploads[0][3]]['content'] == b'selected'
        assert not refreshed.uploads
        manager.enqueue_upload(source)
        assert manager.wait_idle(2)
        assert len(refreshed.uploads) == 1
    finally:
        release.set()
        manager.shutdown()


def test_qa_journal_symlink_is_rejected_without_opening_target(synthetic):
    from electridrive import qa_policy as qa
    policy, drive, rid, remote = synthetic
    target = policy.local_root / 'do-not-open'
    target.write_bytes(b'unchanged')
    path = policy.profile / 'state/electridrive/queue.sqlite3'
    path.symlink_to(target)
    with pytest.raises(qa.QaSafetyError):
        TransferManager(drive, journal_path=path, account_key='a')
    assert target.read_bytes() == b'unchanged'
    assert not list(path.parent.glob('queue.sqlite3.*'))
