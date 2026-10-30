"""End-to-end artificial QA sync and startup regressions; no Google connection."""
from dataclasses import replace
import json
import pytest
from fakes import FakeDrive
from electridrive import qa_policy as qa
from electridrive.config import SyncPair, get_paths
from electridrive.storage.database import SyncDatabase
from electridrive.sync.twoway import TwoWaySyncEngine


@pytest.fixture
def synthetic(monkeypatch, tmp_path):
    monkeypatch.setattr(qa, '_active', None)
    monkeypatch.delenv('ELECTRIDRIVE_QA_PROFILE', raising=False)
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path/'ordinary-config'))
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path/'ordinary-state'))
    p = qa.initialize(tmp_path/'profile', tmp_path/'synthetic')
    monkeypatch.setenv('ELECTRIDRIVE_QA_PROFILE', str(p.profile))
    p = qa.current_policy()
    class ArtificialDrive(FakeDrive):
        def _to_remote(self, node):
            return replace(super()._to_remote(node), app_properties=node.get('app_properties', {}))
    fake = ArtificialDrive()
    rid = fake.add_folder('ElectriDrive-QA-synthetic')
    fake.nodes[rid]['app_properties'] = {'electridrive_qa_nonce': p.nonce}
    remote = p.register_new_root(fake.get_metadata(rid), 'ElectriDrive-QA-synthetic')
    return p, fake, rid, remote


@pytest.mark.parametrize('direction', ['up_only', 'down_only', 'two_way'])
def test_all_directions_converge_only_within_synthetic_roots(synthetic, direction):
    p, fake, rid, remote = synthetic
    (p.local_root/'local.txt').write_bytes(b'artificial local')
    fid = fake.add_file('cloud.txt', b'artificial cloud', parent=rid)
    outside = fake.add_file('personal-fixture.txt', b'never touched')
    original = dict(fake.nodes[outside])
    pair = SyncPair(str(p.local_root), remote, direction=direction, delete_policy='off')
    db = SyncDatabase(get_paths().database_file)
    try:
        engine = TwoWaySyncEngine(fake, db, pair)
        first = engine.run()
        assert first.failed == 0, first.errors
        assert first.uploaded == (direction != 'down_only')
        assert first.downloaded == (direction != 'up_only')
        assert not first.trashed_local and not first.trashed_remote
        second = engine.run()
        assert second.failed == 0, second.errors
        assert second.uploaded == second.downloaded == 0
        assert fake.nodes[fid]['content'] == b'artificial cloud'
        assert fake.nodes[outside] == original
        assert not fake.change_requests  # QA deliberately avoids global Changes API
        assert (p.local_root/'local.txt').read_bytes() == b'artificial local'
    finally:
        db.close()


def test_tampered_disk_settings_stop_engine_even_with_safe_in_memory_pair(synthetic):
    p, fake, rid, remote = synthetic
    settings = get_paths().settings_file
    settings.write_text(json.dumps({'scope':'file', 'sync_pairs':[
        {'local_path':str(p.profile.parent/'outside'), 'remote_folder':remote,
         'enabled':False, 'delete_policy':'off'}]}))
    with pytest.raises(qa.QaSafetyError):
        TwoWaySyncEngine(fake, object(), SyncPair(str(p.local_root), remote, delete_policy='off')).run()
    assert not fake.uploads and not fake.updates


@pytest.mark.parametrize('command', ['sync-up', 'sync', 'sync-configured'])
def test_cli_sync_commands_use_only_artificial_qa_data(synthetic, monkeypatch, command):
    from electridrive import cli
    from electridrive.config import Settings, save_settings
    p, fake, rid, remote = synthetic
    (p.local_root/'artificial.txt').write_bytes(b'qa cli data')
    monkeypatch.setattr(cli, 'GoogleDriveClient', lambda: fake)
    monkeypatch.setattr(cli, 'configure_logging', lambda: None)
    if command == 'sync-configured':
        save_settings(Settings(sync_pairs=[SyncPair(str(p.local_root), remote,
            direction='up_only', delete_policy='off')]))
        arguments = [command]
    else:
        arguments = [command, str(p.local_root), '--remote-folder', remote]
    assert cli.main(arguments) == 0
    assert len(fake.uploads) == 1
    assert fake.nodes[fake.uploads[0][3]]['content'] == b'qa cli data'
    assert all(item[1] == rid for item in fake.uploads)


@pytest.mark.parametrize('sync_all', [False, True])
@pytest.mark.parametrize('tamper_after_queue', [False, True])
def test_gui_sync_jobs_recheck_queued_sources_and_clear_busy(synthetic, monkeypatch, sync_all, tamper_after_queue):
    from types import SimpleNamespace
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QCoreApplication, QEvent
    from electridrive.config import Settings
    from electridrive.ui.views import sync_view
    from electridrive.ui.theme import get_palette
    from electridrive.ui.session import _Worker
    p, fake, rid, remote = synthetic
    source = p.local_root/'selected'
    source.mkdir()
    (source/'artificial.txt').write_bytes(b'qa GUI data')
    pair = SyncPair(str(source), remote, direction='up_only', delete_policy='off')
    app = QApplication.instance() or QApplication([])
    queue, warnings = [], []
    def submit(work, done, fail):
        worker = _Worker(work)
        worker.signals.result.connect(done)
        worker.signals.error.connect(fail)
        queue.append(worker)
    db = SyncDatabase(get_paths().database_file)
    session = SimpleNamespace(client=fake, db=db, settings=Settings(sync_pairs=[pair]), submit=submit)
    monkeypatch.setattr(sync_view, 'show_readable_warning', lambda *args: warnings.append(args))
    monkeypatch.setattr(sync_view, 'show_readable_information', lambda *args: None)
    view = sync_view.SyncView(session, get_palette('electric_dark'))
    try:
        if sync_all:
            view._sync_all(view._sync_all_button)
        else:
            view._sync_now(pair)
        assert view._sync_busy and len(queue) == 1
        if tamper_after_queue:
            source.rename(p.local_root/'original')
            outside = p.profile.parent/'outside-fixture'
            outside.mkdir()
            (outside/'personal-fixture.txt').write_bytes(b'never upload')
            source.symlink_to(outside, target_is_directory=True)
        queue.pop().run()
        assert not view._sync_busy
        if tamper_after_queue:
            assert warnings and not fake.uploads
        else:
            assert not warnings and len(fake.uploads) == 1
            assert fake.nodes[fake.uploads[0][3]]['content'] == b'qa GUI data'
    finally:
        view.close()
        view.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()
        db.close()
