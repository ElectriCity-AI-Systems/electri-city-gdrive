import os
import threading
import time
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from PySide6.QtWidgets import QApplication
from electridrive.google_api.client import FileListing
from electridrive.ui.session import DriveSession
from electridrive.ui.views.explorer_view import ExplorerView
from electridrive.ui.theme import ELECTRIC_DARK
from fakes import FakeDrive


@pytest.fixture
def setup(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    for key in ('XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_STATE_HOME'):
        monkeypatch.setenv(key, str(tmp_path / key))
    session = DriveSession()
    drive = FakeDrive()
    session._on_connected((drive, drive.get_about()))
    view = ExplorerView(session, ELECTRIC_DARK)
    yield app, session, drive, view
    session.shutdown()
    from shiboken6 import isValid
    if isValid(view):
        view.deleteLater()
    app.processEvents()


def test_explorer_lists_and_caches_all_pages(setup):
    app, session, drive, view = setup
    a, b = drive.add_file('first'), drive.add_file('second')
    requests = []
    def listing(parent, page_token=None):
        requests.append(page_token)
        return FileListing([drive.get_metadata(a if page_token is None else b)],
                           'next' if page_token is None else None)
    drive.list_folder = listing
    view.load()
    session.pool.waitForDone()
    app.processEvents()
    assert view.model.rowCount() == 2
    assert requests == [None, 'next']
    assert len(session.db.get_cached_children('root')) == 2


def test_upload_planning_returns_to_gui_before_remote_resolution(setup, tmp_path):
    app, session, drive, view = setup
    entered, release = threading.Event(), threading.Event()
    def find(*args):
        entered.set()
        release.wait(2)
        return None
    drive.find_folder = find
    folder = tmp_path / 'folder'
    folder.mkdir()
    (folder / 'a').write_bytes(b'a')
    try:
        before = time.monotonic()
        view._upload_paths([str(folder)])
        elapsed = time.monotonic() - before
        assert elapsed < .1
        assert entered.wait(1)
        # GUI delivery is available while planning is still blocked.
        app.processEvents()
    finally:
        release.set()
        session.pool.waitForDone()


def test_signout_cancels_planner_before_it_can_enqueue(setup, tmp_path, monkeypatch):
    app, session, drive, view = setup
    entered, release = threading.Event(), threading.Event()
    def find(*args):
        entered.set()
        release.wait(2)
        return None
    drive.find_folder = find
    folder = tmp_path / 'folder'
    folder.mkdir()
    (folder / 'a').write_bytes(b'a')
    view._upload_paths([str(folder)])
    assert entered.wait(1)
    monkeypatch.setattr('electridrive.google_api.oauth.clear_credentials', lambda: None)
    threading.Timer(.05, release.set).start()
    session.sign_out()
    assert not drive.uploads
    assert len(drive.nodes) == 1


def test_old_navigation_cannot_cache_under_new_parent_or_show_stale_error(setup, monkeypatch):
    app, session, drive, view = setup
    queue, warnings = [], []
    monkeypatch.setattr(session, 'submit', lambda work, done, fail: queue.append((work, done, fail)))
    monkeypatch.setattr('electridrive.ui.views.explorer_view.QMessageBox.warning',
                        lambda *a: warnings.append(a[-1]))
    first = drive.add_file('first')
    folder = drive.add_folder('folder')
    view.load()
    view._stack.append((folder, 'folder'))
    view.load()
    queue[0][1]([drive.get_metadata(first)])
    queue[0][2]('old request error')
    assert not session.db.get_cached_children(folder)
    assert not session.db.get_cached_children('root')
    assert view.model.rowCount() == 0 and warnings == []


def test_deleted_explorer_does_not_receive_pending_navigation_callbacks(setup, monkeypatch):
    from PySide6.QtCore import QCoreApplication, QEvent
    app, session, drive, view = setup
    queue = []
    monkeypatch.setattr(session, 'submit', lambda work, done, fail: queue.append((work, done, fail)))
    view.load()
    view.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    # A queued result must become harmless after its view has been destroyed.
    queue[0][1]([])
    queue[0][2]('obsolete')
