import gc
import os
import threading
import time
import weakref
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton
from electridrive.config import SyncPair
from electridrive.ui.session import DriveSession
from electridrive.ui.theme import ELECTRIC_DARK
from fakes import FakeDrive


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def session(tmp_path, monkeypatch, app):
    for key in ('XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_STATE_HOME'):
        monkeypatch.setenv(key, str(tmp_path / key))
    s = DriveSession()
    yield s
    s.shutdown()
    app.processEvents()


def drain(app, predicate):
    end = time.monotonic() + 3
    while not predicate() and time.monotonic() < end:
        app.processEvents()
        time.sleep(.005)
    assert predicate()


def test_completed_workers_are_released(session, app):
    for _ in range(20):
        session.submit(lambda: 1)
    refs = [weakref.ref(w) for w in session._pending]
    session.pool.waitForDone()
    drain(app, lambda: not session._pending)
    for _ in range(4):
        app.processEvents()
        gc.collect()
    assert not any(ref() is not None for ref in refs)


def test_signout_drains_jobs_before_clear_and_suppresses_old_results(session, app, monkeypatch):
    entered = threading.Event()
    released = threading.Event()
    results = []
    def work():
        entered.set()
        released.wait(2)
        session.db.get_meta('missing')
    session.submit(work, results.append)
    assert entered.wait(1)
    observed = []
    monkeypatch.setattr('electridrive.google_api.oauth.clear_credentials',
                        lambda: observed.append(released.is_set()))
    threading.Timer(.05, released.set).start()
    session.sign_out()
    app.processEvents()
    assert observed == [True]
    assert results == []
    assert not session._pending


def test_reload_keeps_manager_and_rejects_changed_account(session, app, monkeypatch):
    drive = FakeDrive()
    session._on_connected((drive, drive.get_about()))
    manager = session.transfers
    monkeypatch.setattr('electridrive.google_api.client.GoogleDriveClient', lambda: drive)
    session.reload_client()
    session.pool.waitForDone()
    app.processEvents()
    assert session.transfers is manager
    other = FakeDrive()
    other.get_about = lambda: SimpleNamespace(user_email='someone-else@example.com')
    monkeypatch.setattr('electridrive.google_api.client.GoogleDriveClient', lambda: other)
    errors = []
    session.reload_client(on_error=errors.append)
    session.pool.waitForDone()
    app.processEvents()
    assert errors and "account" in errors[0]
    assert session.transfers is manager



def test_picker_different_account_never_saves_credentials(session, app, monkeypatch):
    drive = FakeDrive()
    session._on_connected((drive, drive.get_about()))
    result = SimpleNamespace(credentials=object(), file_ids=['one'])
    monkeypatch.setattr('electridrive.google_api.picker.pick_from_drive', lambda **kw: result)
    other = FakeDrive()
    other.get_about = lambda: SimpleNamespace(user_email='other@example.com')
    monkeypatch.setattr('electridrive.google_api.client.GoogleDriveClient', lambda **kw: other)
    saved, errors = [], []
    monkeypatch.setattr('electridrive.google_api.oauth.save_credentials', saved.append)
    session.pick_from_drive(on_error=errors.append)
    session.pool.waitForDone()
    app.processEvents()
    assert errors and 'account' in errors[0].lower()
    assert saved == []
    assert session.client is drive



def test_shutdown_waits_for_database_users(session):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    def work():
        entered.set()
        release.wait(2)
        session.db.set_meta('drained', 'yes')
        finished.set()
    session.submit(work)
    assert entered.wait(1)
    threading.Timer(.05, release.set).start()
    session.shutdown()
    assert finished.is_set()



def test_inaccessible_keyring_does_not_crash_login_startup(session, monkeypatch):
    from electridrive.google_api.oauth import OAuthError
    def unavailable():
        raise OAuthError('Keyring unavailable')
    monkeypatch.setattr('electridrive.google_api.oauth.has_saved_token', unavailable)
    assert session.has_token() is False


def test_reload_account_lookup_runs_in_background(session, app, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    drive = FakeDrive()
    session._on_connected((drive, drive.get_about()))
    def connect():
        entered.set()
        release.wait(2)
        return drive, drive.get_about()
    monkeypatch.setattr(session, '_do_connect', connect)
    try:
        before = time.monotonic()
        session.reload_client()
        assert time.monotonic() - before < .1
        assert entered.wait(1)
    finally:
        release.set()
        session.pool.waitForDone()


def test_window_close_failure_keeps_window_open(app, monkeypatch):
    from PySide6.QtGui import QCloseEvent
    from PySide6.QtWidgets import QMainWindow, QMessageBox
    from electridrive.ui.main_window import MainWindow
    window = QMainWindow()
    def fail():
        raise RuntimeError('mount still busy')
    window.session = SimpleNamespace(shutdown=fail)
    errors = []
    monkeypatch.setattr(QMessageBox, 'warning', lambda *args: errors.append(args[-1]))
    event = QCloseEvent()
    MainWindow.closeEvent(window, event)
    assert not event.isAccepted()
    assert errors == ['mount still busy']


def test_session_restart_shows_saved_transfer_history(session, app, tmp_path):
    from electridrive.ui.views.transfers_view import TransfersView
    drive = FakeDrive()
    session._on_connected((drive, drive.get_about()))
    path = tmp_path / 'source'
    path.write_bytes(b'content')
    task_id = session.transfers.enqueue_upload(path)[0]
    assert session.transfers.wait_idle(2)
    session.shutdown()
    second = DriveSession()
    try:
        second._on_connected((drive, drive.get_about()))
        saved = second.transfers.get(task_id)
        assert saved is not None and saved.state.value == 'done'
        view = TransfersView(second, ELECTRIC_DARK)
        assert task_id in view._rows
        view.deleteLater()
    finally:
        second.shutdown()


def test_interrupted_upload_retry_requires_duplicate_warning(session, app, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from electridrive.ui.views.transfers_view import TransfersView
    drive = FakeDrive()
    session._on_connected((drive, drive.get_about()))
    source = tmp_path / 'source'
    source.write_bytes(b'content')
    original = drive.upload_file
    def fail(*args, **kwargs):
        raise OSError('interrupted')
    drive.upload_file = fail
    task_id = session.transfers.enqueue_upload(source)[0]
    assert session.transfers.wait_idle(2)
    session.transfers.get(task_id).recovered = True
    drive.upload_file = original
    asked = []
    monkeypatch.setattr(QMessageBox, 'question', lambda *args: asked.append(args[-1]) or QMessageBox.No)
    view = TransfersView(session, ELECTRIC_DARK)
    view._retry(task_id)
    assert asked and 'already' in asked[0].lower()
    assert not drive.uploads
    assert len(session.transfers.snapshot()) == 1
    view.deleteLater()


def test_live_account_change_cannot_reuse_another_accounts_queue(session):
    first = FakeDrive()
    first.account_key = 'account-a'
    session._on_connected((first, first.get_about()))
    manager = session.transfers
    other = FakeDrive()
    other.account_key = 'account-b'
    errors = []
    session.connect_failed.connect(errors.append)
    session._on_connected((other, other.get_about()))
    assert errors and 'account' in errors[0].lower()
    assert session.client is first
    assert session.transfers is manager
    assert session.account_key == 'account-a'


def test_journal_open_failure_leaves_session_disconnected(session, monkeypatch):
    drive = FakeDrive()
    def fail(*args, **kwargs):
        raise OSError('queue unavailable')
    monkeypatch.setattr('electridrive.transfers.journal.TransferJournal', fail)
    errors = []
    session.connect_failed.connect(errors.append)
    session._on_connected((drive, drive.get_about()))
    assert errors and 'queue unavailable' in errors[0]
    assert not session.is_connected
    assert session.transfers is None


def test_signout_restores_only_the_current_accounts_history(session, tmp_path, monkeypatch):
    monkeypatch.setattr('electridrive.google_api.oauth.clear_credentials', lambda: None)
    first = FakeDrive()
    first.account_key = 'account-a'
    other = FakeDrive()
    other.account_key = 'account-b'
    session._on_connected((first, first.get_about()))
    source = tmp_path / 'source'
    source.write_bytes(b'content')
    first_id = session.transfers.enqueue_upload(source)[0]
    assert session.transfers.wait_idle(2)
    session.sign_out()
    session._on_connected((other, other.get_about()))
    assert session.transfers.snapshot() == []
    session.sign_out()
    session._on_connected((first, first.get_about()))
    assert [t.id for t in session.transfers.snapshot()] == [first_id]


def test_sync_owner_survives_deleted_view_and_preserves_report(session, app, monkeypatch, tmp_path):
    from PySide6.QtCore import QCoreApplication, QEvent
    from electridrive.sync.twoway import SyncReport
    from electridrive.ui.views import sync_view
    entered, release = threading.Event(), threading.Event()
    pair = SyncPair(str(tmp_path), 'remote')
    session.settings.sync_pairs = [pair]
    session.client = FakeDrive()
    calls = []
    class Engine:
        def __init__(self, client, db, pair, cancel_event=None):
            self.cancel = cancel_event
        def run(self):
            calls.append(1)
            entered.set()
            release.wait(2)
            return SyncReport(uploaded=1, failed=1, errors=['offline'])
    monkeypatch.setattr('electridrive.sync.twoway.TwoWaySyncEngine', Engine)
    monkeypatch.setattr(sync_view, 'show_readable_warning', lambda *a: None)
    first = sync_view.SyncView(session, ELECTRIC_DARK)
    first._sync_now(pair)
    assert entered.wait(1)
    first.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    second = sync_view.SyncView(session, ELECTRIC_DARK)
    try:
        second._sync_now(pair)
        assert second._sync_busy
        release.set()
        session.pool.waitForDone()
        drain(app, lambda: not second._sync_busy)
        assert calls == [1]
        assert second._report_for_pair(pair).errors == ['offline']
    finally:
        release.set()
        second.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def test_signout_cancels_sync_before_db_release(session, app, monkeypatch, tmp_path):
    from electridrive.ui.views import sync_view
    entered, canceled = threading.Event(), threading.Event()
    pair = SyncPair(str(tmp_path), 'remote')
    session.client = FakeDrive()
    session.settings.sync_pairs = [pair]
    class Engine:
        def __init__(self, client, db, pair, cancel_event=None):
            self.cancel = cancel_event
        def run(self):
            entered.set()
            if self.cancel and self.cancel.wait(1):
                canceled.set()
    monkeypatch.setattr('electridrive.sync.twoway.TwoWaySyncEngine', Engine)
    monkeypatch.setattr('electridrive.google_api.oauth.clear_credentials', lambda: None)
    view = sync_view.SyncView(session, ELECTRIC_DARK)
    view._sync_now(pair)
    assert entered.wait(1)
    session.sign_out()
    assert canceled.is_set()
    view.deleteLater()


def test_failed_unmount_blocks_signout_and_preserves_account(session, monkeypatch):
    drive = FakeDrive()
    session._on_connected((drive, drive.get_about()))
    manager = session.transfers
    def fail():
        raise RuntimeError('busy mount')
    mount = SimpleNamespace(is_mounted=True, stop=fail)
    session.fuse_mount = mount
    effects = []
    monkeypatch.setattr('electridrive.google_api.oauth.clear_credentials', lambda: effects.append(1))
    try:
        with pytest.raises(RuntimeError, match='busy mount'):
            session.sign_out()
        assert session.client is drive and session.transfers is manager
        assert session.fuse_mount is mount and effects == []
        assert not session._accepting
    finally:
        session.fuse_mount = None


def test_rejected_executor_submission_releases_pending_callbacks(session, monkeypatch):
    def unavailable(worker):
        raise RuntimeError('executor unavailable')
    monkeypatch.setattr(session.pool, 'start', unavailable)
    with pytest.raises(RuntimeError, match='executor unavailable'):
        session.submit(lambda: 1, lambda value: None)
    assert not session._pending and not session._callbacks


def test_old_transfer_events_do_not_reach_new_account_views(session, app, tmp_path, monkeypatch):
    from electridrive.transfers.manager import Transfer, TransferKind
    drive = FakeDrive()
    session._on_connected((drive, drive.get_about()))
    old = session.transfers
    monkeypatch.setattr('electridrive.google_api.oauth.clear_credentials', lambda: None)
    events = []
    session.transfer_changed.connect(events.append)
    session.sign_out()
    old._on_update(Transfer(123, TransferKind.UPLOAD, 'old-account-file'))
    app.processEvents()
    assert events == []


from test_qa_integration import synthetic


def test_qa_session_and_refresh_never_open_transfer_journal(synthetic, app, monkeypatch):
    from electridrive import qa_policy as qa
    policy, drive, rid, remote = synthetic
    session = DriveSession()
    try:
        assert session._on_connected((drive, drive.get_about()))
        manager = session.transfers
        assert manager._journal is None
        assert not (policy.profile / 'state/electridrive/transfer_queue.sqlite3').exists()
        monkeypatch.setattr(session, '_do_connect', lambda: (drive, drive.get_about()))
        session.reload_client()
        session.pool.waitForDone()
        app.processEvents()
        assert session.transfers is manager
        with pytest.raises(qa.QaSafetyError):
            session.client.list_files()
        assert manager._journal is None
    finally:
        session.shutdown()
