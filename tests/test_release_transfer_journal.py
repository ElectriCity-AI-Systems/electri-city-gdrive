import multiprocessing
import os
from pathlib import Path

from electridrive.transfers import TransferManager
from fakes import FakeDrive


def _crash_upload(journal_path, source):
    class InterruptedDrive(FakeDrive):
        def upload_file(self, *args, **kwargs):
            os._exit(0)  # simulate process loss while the remote outcome is unknown
    manager = TransferManager(InterruptedDrive(), journal_path=journal_path,
                              account_key='account-a', max_workers=1)
    manager.enqueue_upload(source)
    manager.wait_idle(3)
    os._exit(2)


def test_completed_history_and_remote_id_survive_restart(tmp_path):
    source = tmp_path / 'source'
    source.write_bytes(b'content')
    journal = tmp_path / 'queue.sqlite3'
    drive = FakeDrive()
    first = TransferManager(drive, journal_path=journal, account_key='account-a')
    upload_id = first.enqueue_upload(source)[0]
    assert first.wait_idle(2)
    remote_id = first.get(upload_id).file_id
    first.shutdown()
    second = TransferManager(drive, journal_path=journal, account_key='account-a')
    try:
        restored = second.get(upload_id)
        assert restored.state.value == 'done'
        assert restored.file_id == remote_id
        assert restored.local_path == source
        assert restored.total == len(b'content')
        assert len(drive.uploads) == 1
        later_id = second.enqueue_upload(source)[0]
        assert later_id > upload_id
        assert second.wait_idle(2)
    finally:
        second.shutdown()


def test_process_interrupted_upload_requires_manual_retry(tmp_path):
    source = tmp_path / 'source'
    source.write_bytes(b'content')
    journal = tmp_path / 'queue.sqlite3'
    process = multiprocessing.get_context('spawn').Process(target=_crash_upload,
                                                           args=(journal, source))
    process.start()
    process.join(5)
    assert process.exitcode == 0
    drive = FakeDrive()
    restored = TransferManager(drive, journal_path=journal, account_key='account-a')
    try:
        tasks = restored.snapshot()
        assert len(tasks) == 1
        task = tasks[0]
        assert task.state.value == 'failed'
        assert task.recovered
        assert 'already' in task.error.lower()
        assert restored.wait_idle(.05)
        restored.start()
        assert not drive.uploads
        retried = restored.retry(task.id)
        assert retried > task.id
        assert restored.wait_idle(2)
        assert len(drive.uploads) == 1
    finally:
        restored.shutdown()


def test_history_is_account_scoped_with_unique_ids(tmp_path):
    source = tmp_path / 'source'
    source.write_bytes(b'content')
    journal = tmp_path / 'queue.sqlite3'
    ids = []
    for account in ('account-a', 'account-b'):
        manager = TransferManager(FakeDrive(), journal_path=journal, account_key=account)
        try:
            assert manager.snapshot() == []
            ids.append(manager.enqueue_upload(source)[0])
            assert manager.wait_idle(2)
        finally:
            manager.shutdown()
    assert ids[0] != ids[1]
    manager = TransferManager(FakeDrive(), journal_path=journal, account_key='account-a')
    try:
        assert [t.id for t in manager.snapshot()] == [ids[0]]
        assert manager.get(ids[1]) is None
    finally:
        manager.shutdown()


def test_download_destination_reservation_survives_restart(tmp_path):
    drive = FakeDrive()
    remote_id = drive.add_file('file', b'content')
    journal = tmp_path / 'queue.sqlite3'
    first = TransferManager(drive, journal_path=journal, account_key='account-a')
    task_id = first.enqueue_download(drive.get_metadata(remote_id), tmp_path / 'dest')[0]
    assert first.wait_idle(2)
    saved_path = first.get(task_id).dest_path
    first.shutdown()
    saved_path.unlink()  # removing the bytes does not remove the historical reservation
    second = TransferManager(drive, journal_path=journal, account_key='account-a')
    try:
        next_id = second.enqueue_download(drive.get_metadata(remote_id), tmp_path / 'dest')[0]
        assert second.wait_idle(2)
        assert second.get(next_id).dest_path != saved_path
        assert second.get(task_id).dest_path == saved_path
    finally:
        second.shutdown()


def test_journal_write_failure_refuses_network_and_keeps_worker_reusable(tmp_path, monkeypatch):
    import sqlite3
    source = tmp_path / 'source'
    source.write_bytes(b'content')
    drive = FakeDrive()
    manager = TransferManager(drive, journal_path=tmp_path / 'queue.sqlite3', account_key='a', max_workers=1)
    save = manager._journal.save
    def unavailable(*args):
        raise sqlite3.OperationalError('disk unavailable')
    monkeypatch.setattr(manager._journal, 'save', unavailable)
    try:
        task = manager.enqueue_upload(source)[0]
        assert manager.wait_idle(2)
        assert manager.get(task).state.value == 'failed'
        assert not drive.uploads
        monkeypatch.setattr(manager._journal, 'save', save)
        retried = manager.retry(task)
        assert manager.wait_idle(2)
        assert manager.get(retried).state.value == 'done'
    finally:
        manager.shutdown()


def test_second_live_queue_cannot_recover_another_managers_jobs(tmp_path):
    import pytest
    journal = tmp_path / 'queue.sqlite3'
    manager = TransferManager(FakeDrive(), journal_path=journal, account_key='a')
    try:
        with pytest.raises(RuntimeError, match='already open'):
            TransferManager(FakeDrive(), journal_path=journal, account_key='a')
    finally:
        manager.shutdown()


def _crash_after_remote_id(journal_path, source):
    holder = {}
    def changed(task):
        if task.file_id and task.state.value == 'running':
            os._exit(0)
    manager = TransferManager(FakeDrive(), journal_path=journal_path,
                              account_key='account-a', on_update=changed, max_workers=1)
    holder['manager'] = manager
    manager.enqueue_upload(source)
    manager.wait_idle(3)
    os._exit(2)


def test_remote_id_is_retained_when_process_dies_before_done(tmp_path):
    source = tmp_path / 'source'
    source.write_bytes(b'content')
    journal = tmp_path / 'queue.sqlite3'
    process = multiprocessing.get_context('spawn').Process(target=_crash_after_remote_id,
                                                           args=(journal, source))
    process.start()
    process.join(5)
    assert process.exitcode == 0
    drive = FakeDrive()
    manager = TransferManager(drive, journal_path=journal, account_key='account-a')
    try:
        task = manager.snapshot()[0]
        assert task.state.value == 'failed' and task.recovered
        assert task.file_id == 'file-1'
        assert not drive.uploads
    finally:
        manager.shutdown()


def test_export_format_and_destination_survive_restart(tmp_path):
    drive = FakeDrive()
    remote = drive.add_file('memo', b'document', mime='application/vnd.google-apps.document')
    journal = tmp_path / 'queue.sqlite3'
    first = TransferManager(drive, journal_path=journal, account_key='account-a')
    transfer_id = first.enqueue_download(drive.get_metadata(remote), tmp_path / 'dest')[0]
    assert first.wait_idle(2)
    first.shutdown()
    second = TransferManager(drive, journal_path=journal, account_key='account-a')
    try:
        task = second.get(transfer_id)
        assert task.kind.value == 'export'
        assert task.file_id == remote
        assert task.export_mime == 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
        assert task.dest_path.read_bytes() == b'EXPORTED:document'
        assert task.state.value == 'done'
    finally:
        second.shutdown()


def test_saved_upload_retry_refuses_a_source_replaced_with_symlink(tmp_path):
    source = tmp_path / 'selected'
    source.write_bytes(b'selected')
    outside = tmp_path / 'private'
    outside.write_bytes(b'private-content')
    journal = tmp_path / 'queue.sqlite3'
    first_drive = FakeDrive()
    def fail(*args, **kwargs):
        raise OSError('offline')
    first_drive.upload_file = fail
    first = TransferManager(first_drive, journal_path=journal, account_key='a')
    task_id = first.enqueue_upload(source)[0]
    assert first.wait_idle(2)
    first.shutdown()
    source.unlink()
    source.symlink_to(outside)
    drive = FakeDrive()
    second = TransferManager(drive, journal_path=journal, account_key='a')
    try:
        retried = second.retry(task_id)
        assert second.wait_idle(2)
        assert second.get(retried).state.value == 'failed'
        assert not drive.uploads
    finally:
        second.shutdown()
