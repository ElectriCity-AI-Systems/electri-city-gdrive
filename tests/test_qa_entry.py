from electridrive import cli, qa_policy as qa


def test_qa_init_precedes_logging(tmp_path, monkeypatch):
    profile, root = tmp_path / 'profile', tmp_path / 'synthetic'
    monkeypatch.setattr(cli, 'configure_logging', lambda: (_ for _ in ()).throw(AssertionError('logging during init')))
    assert cli.main(['qa-init', '--profile', str(profile), '--local-root', str(root)]) == 0
    assert (profile / qa.POLICY).exists()


def test_qa_mount_stops_before_fuse(monkeypatch):
    monkeypatch.setattr(qa, 'current_policy', lambda: object())
    assert cli.cmd_mount(cli.argparse.Namespace()) == 2


def test_cli_bootstrap_before_logging(monkeypatch):
    seen = []
    monkeypatch.setattr(qa, 'bootstrap_profile', lambda argv: seen.append('bootstrap') or argv)
    monkeypatch.setattr(qa, 'check_startup', lambda: seen.append('startup'))
    monkeypatch.setattr(cli, 'configure_logging', lambda: seen.append('logging'))
    monkeypatch.setattr(cli, 'cmd_doctor', lambda args: 0)
    assert cli.main(['doctor']) == 0
    assert seen.index('bootstrap') < seen.index('logging')
    assert 'startup' in seen


def test_worker_rechecks_before_invoking_job(monkeypatch):
    from electridrive.ui.session import _Worker
    called = []
    def stop():
        raise qa.QaSafetyError('changed policy')
    monkeypatch.setattr(qa, 'check_startup', stop)
    worker = _Worker(lambda: called.append('unsafe job'))
    worker.run()
    assert called == []


def test_gui_rejects_outside_local_before_remote_dialog(tmp_path, monkeypatch):
    import os
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from types import SimpleNamespace
    from PySide6.QtWidgets import QApplication
    from electridrive.config import Settings
    from electridrive.ui.views import sync_view
    from electridrive.ui.theme import get_palette
    app = QApplication.instance() or QApplication([])
    class Policy:
        local_root = tmp_path
        def local(self, value):
            raise qa.QaSafetyError('outside synthetic root')
    monkeypatch.setattr(qa, 'current_policy', lambda: Policy())
    monkeypatch.setattr(sync_view.QFileDialog, 'getExistingDirectory', lambda *a: '/outside')
    monkeypatch.setattr(sync_view.QInputDialog, 'getItem', lambda *a: (_ for _ in ()).throw(AssertionError('remote dialog opened')))
    monkeypatch.setattr(sync_view, 'show_readable_warning', lambda *a: None)
    session = SimpleNamespace(settings=Settings(), client=None)
    view = sync_view.SyncView(session, get_palette('electric_dark'))
    view._add_pair()
    assert session.settings.sync_pairs == []
    view.close()
    view.deleteLater()
    from PySide6.QtCore import QCoreApplication, QEvent
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()


def test_rejected_full_scope_preserves_live_settings_and_credentials(monkeypatch):
    import pytest
    from types import SimpleNamespace
    from electridrive.config import Settings
    from electridrive.ui.session import DriveSession
    from electridrive.google_api import oauth
    monkeypatch.setattr(qa, 'current_policy', lambda: object())
    effects = []
    monkeypatch.setattr(oauth, 'clear_credentials', lambda: effects.append('clear'))
    session = SimpleNamespace(settings=Settings(scope='file'), save=lambda: effects.append('save'))
    with pytest.raises(qa.QaSafetyError):
        DriveSession.set_access_mode(session, 'drive')
    assert session.settings.scope == 'file'
    assert effects == []


def test_qa_login_and_settings_explain_supported_setup(tmp_path, monkeypatch):
    import os
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from types import SimpleNamespace
    from PySide6.QtWidgets import QApplication, QLabel
    from electridrive.config import Settings
    from electridrive.ui.views.login_view import LoginView
    from electridrive.ui.views.settings_view import SettingsView
    from electridrive.ui.theme import get_palette
    from electridrive.google_api import app_client
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(qa, 'current_policy', lambda: object())
    monkeypatch.setattr(app_client, 'is_configured', lambda: False)
    session = SimpleNamespace(settings=Settings(), about=None,
        paths=SimpleNamespace(config_dir=tmp_path, state_dir=tmp_path, cache_dir=tmp_path),
        has_credentials=lambda: False)
    login = LoginView(session, get_palette('electric_dark'))
    settings = SettingsView(session, get_palette('electric_dark'))
    try:
        text = '\n'.join(label.text() for label in login.findChildren(QLabel))
        assert 'ELECTRIDRIVE_CLIENT_ID' not in text
        assert str(tmp_path / 'app_client.json') in text
        assert 'isolated' in text
        assert 'Drive picker' not in text and 'Mount your granted' not in text
        assert not settings._mode_combo.isEnabled()
        assert any('QA' in label.text() and 'per-file' in label.text()
                   for label in settings.findChildren(QLabel))
    finally:
        from PySide6.QtCore import QCoreApplication, QEvent
        for view in (login, settings):
            view.close()
            view.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()


def test_no_roots_guidance_requires_close_create_restart(tmp_path, monkeypatch):
    import os
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from types import SimpleNamespace
    from PySide6.QtWidgets import QApplication
    from electridrive.config import Settings
    from electridrive.ui.views import sync_view
    from electridrive.ui.theme import get_palette
    app = QApplication.instance() or QApplication([])
    policy = SimpleNamespace(local_root=tmp_path, remote_ids=(), local=lambda path: path)
    monkeypatch.setattr(qa, 'current_policy', lambda: policy)
    monkeypatch.setattr(sync_view.QFileDialog, 'getExistingDirectory', lambda *args: str(tmp_path))
    monkeypatch.setattr(sync_view.QInputDialog, 'getItem', lambda *args: (_ for _ in ()).throw(AssertionError('no roots to choose')))
    messages = []
    monkeypatch.setattr(sync_view, 'show_readable_information', lambda *args: messages.append(args[-1]))
    session = SimpleNamespace(settings=Settings(), client=None)
    view = sync_view.SyncView(session, get_palette('electric_dark'))
    try:
        view._add_pair()
        assert len(messages) == 1
        assert 'Close' in messages[0] and 'restart' in messages[0]
        assert 'qa-create-folder' in messages[0]
        assert session.settings.sync_pairs == []
    finally:
        from PySide6.QtCore import QCoreApplication, QEvent
        view.close()
        view.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()
