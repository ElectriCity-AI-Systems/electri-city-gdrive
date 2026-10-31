from pathlib import Path
import threading
import pytest

from electridrive.config import SyncPair
from electridrive.storage.database import SyncDatabase
from electridrive.sync.twoway import TwoWaySyncEngine
from electridrive.sync.engine import UploadOnlySyncEngine
from electridrive.sync.rules import SyncRules
from electridrive.sync.uploader import plan_upload
from electridrive.sync.downloader import plan_download
from fakes import FakeDrive


def setup_pair(tmp_path):
    root = tmp_path / 'sync'
    root.mkdir()
    fake = FakeDrive()
    db = SyncDatabase(tmp_path / 'state.db')
    fake.account_key = 'account-' + str(id(fake))
    pair = SyncPair(local_path=str(root), remote_folder='Backup', include_all_files=False)
    return root, fake, db, pair


def test_symlink_download_target_aborts_and_keeps_outside(tmp_path):
    root, fake, db, pair = setup_pair(tmp_path)
    remote = fake.ensure_folder_path('Backup')
    fake.add_file('link', b'BAD', remote)
    outside = tmp_path / 'outside'
    outside.write_bytes(b'GOOD')
    (root / 'link').symlink_to(outside)
    report = TwoWaySyncEngine(fake, db, pair).run()
    assert outside.read_bytes() == b'GOOD'
    assert report.trashed_remote == 0


def test_missing_local_root_aborts_before_remote_trash(tmp_path):
    root, fake, db, pair = setup_pair(tmp_path)
    (root / 'a').write_bytes(b'a')
    engine = TwoWaySyncEngine(fake, db, pair)
    engine.run()
    root.rename(tmp_path / 'away')
    assert engine.run().failed > 0
    assert not any(n['trashed'] for n in fake.nodes.values())


def test_missing_bound_remote_root_is_not_recreated(tmp_path):
    root, fake, db, pair = setup_pair(tmp_path)
    (root / 'a').write_bytes(b'a')
    TwoWaySyncEngine(fake, db, pair).run()
    fake.trash(fake.ensure_folder_path('Backup'))
    assert TwoWaySyncEngine(fake, db, pair).run().failed > 0
    assert (root / 'a').exists()
    assert len([n for n in fake.nodes.values() if n['name'] == 'Backup']) == 1


def test_replaced_local_root_requires_explicit_rebind(tmp_path):
    root, fake, db, pair = setup_pair(tmp_path)
    (root / 'a').write_bytes(b'a')
    TwoWaySyncEngine(fake, db, pair).run()
    root.rename(tmp_path / 'old')
    root.mkdir()
    assert TwoWaySyncEngine(fake, db, pair).run().failed > 0
    assert not any(n['trashed'] for n in fake.nodes.values())


def test_account_switch_does_not_use_previous_deletion_history(tmp_path):
    root, fake, db, pair = setup_pair(tmp_path)
    (root / 'a').write_bytes(b'a')
    TwoWaySyncEngine(fake, db, pair).run()
    other = FakeDrive()
    report = TwoWaySyncEngine(other, db, pair).run()
    assert report.trashed_local == 0
    assert (root / 'a').read_bytes() == b'a'
    assert report.uploaded == 1


def test_exclusions_apply_to_remote_and_existing_history(tmp_path):
    root, fake, db, pair = setup_pair(tmp_path)
    remote = fake.ensure_folder_path('Backup')
    fid = fake.add_file('.secret', b'secret', remote)
    engine = TwoWaySyncEngine(fake, db, pair, SyncRules(include_hidden=True))
    assert engine.run().downloaded == 1
    (root / '.secret').unlink()
    report = TwoWaySyncEngine(fake, db, pair).run()
    assert report.downloaded == report.trashed_remote == 0
    assert not fake.nodes[fid]['trashed']


def test_changed_upload_updates_same_remote_id(tmp_path):
    root, fake, db, pair = setup_pair(tmp_path)
    (root / 'a').write_bytes(b'old')
    engine = TwoWaySyncEngine(fake, db, pair)
    engine.run()
    fid = fake.uploads[0][-1]
    (root / 'a').write_bytes(b'new')
    assert engine.run().uploaded == 1
    assert fake.nodes[fid]['content'] == b'new'
    assert len([n for n in fake.nodes.values() if n['name'] == 'a']) == 1


def test_duplicate_remote_names_abort_before_mutations(tmp_path):
    root, fake, db, pair = setup_pair(tmp_path)
    remote = fake.ensure_folder_path('Backup')
    fake.add_file('a', b'one', remote)
    fake.add_file('a', b'two', remote)
    assert TwoWaySyncEngine(fake, db, pair).run().failed > 0
    assert not (root / 'a').exists()


def test_repeated_trash_preserves_both_versions(tmp_path):
    root, fake, db, pair = setup_pair(tmp_path)
    engine = TwoWaySyncEngine(fake, db, pair)
    for content in [b'one', b'two']:
        (root / 'a').write_bytes(content)
        engine.run()
        for node in list(fake.nodes.values()):
            if node['name'] == 'a' and not node['trashed']:
                fake.trash(node['id'])
        assert engine.run().trashed_local == 1
    contents = [p.read_bytes() for p in (root / '.electridrive-trash').rglob('*') if p.is_file()]
    assert sorted(contents) == [b'one', b'two']


def test_partial_download_does_not_replace_original(tmp_path):
    root, fake, db, pair = setup_pair(tmp_path)
    remote = fake.ensure_folder_path('Backup')
    fid = fake.add_file('a', b'old', remote)
    engine = TwoWaySyncEngine(fake, db, pair)
    engine.run()
    fake.modify_file(fid, b'new')
    def interrupted(file_id, dest, progress_cb=None):
        dest.write_bytes(b'PART')
        raise OSError('interrupted')
    fake.download_file = interrupted
    assert engine.run().failed >= 1
    assert (root / 'a').read_bytes() == b'old'


def test_upload_only_state_is_scoped_to_target_and_account(tmp_path):
    root, fake, db, pair = setup_pair(tmp_path)
    (root / 'a').write_bytes(b'bytes')
    engine = UploadOnlySyncEngine(drive_client=fake, database=db)
    assert engine.sync_up(root, 'First').uploaded == 1
    assert engine.sync_up(root, 'Second').uploaded == 1
    assert engine.sync_up(root, 'First').skipped == 1
    other = UploadOnlySyncEngine(drive_client=FakeDrive(), database=db)
    assert other.sync_up(root, 'First').uploaded == 1


def test_cancelled_run_has_no_mutations(tmp_path):
    root, fake, db, pair = setup_pair(tmp_path)
    (root / 'a').write_bytes(b'a')
    event = threading.Event()
    event.set()
    with pytest.raises(RuntimeError, match='[Cc]ancel'):
        TwoWaySyncEngine(fake, db, pair, cancel_event=event).run()
    assert not fake.uploads
    assert len(fake.nodes) == 1


def test_pair_lock_blocks_overlapping_engine_instances(tmp_path):
    root, fake, db, pair = setup_pair(tmp_path)
    (root / 'a').write_bytes(b'a')
    entered = threading.Event()
    release = threading.Event()
    original = fake.upload_file
    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    fake.upload_file = blocked
    errors = []
    def run():
        try:
            TwoWaySyncEngine(fake, db, pair).run()
        except Exception as exc:
            errors.append(exc)
    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert entered.wait(5)
        with pytest.raises(RuntimeError, match='already running'):
            TwoWaySyncEngine(fake, db, pair).run()
    finally:
        release.set()
        worker.join(5)
    assert not errors
    assert len(fake.uploads) == 1


def test_cancel_during_download_keeps_original_and_stops_next_action(tmp_path):
    root, fake, db, pair = setup_pair(tmp_path)
    remote = fake.ensure_folder_path('Backup')
    fid = fake.add_file('a', b'old', remote)
    engine = TwoWaySyncEngine(fake, db, pair)
    engine.run()
    fake.modify_file(fid, b'new')
    fake.add_file('z', b'new', remote)
    event = threading.Event()
    original = fake.download_file
    def cancel(*args, **kwargs):
        event.set()
        return original(*args, **kwargs)
    fake.download_file = cancel
    with pytest.raises(RuntimeError, match='[Cc]ancel'):
        TwoWaySyncEngine(fake, db, pair, cancel_event=event).run()
    assert (root / 'a').read_bytes() == b'old'
    assert not (root / 'z').exists()


def test_repeated_same_timestamp_conflicts_preserve_versions(tmp_path, monkeypatch):
    import electridrive.sync.twoway as module
    root, fake, db, pair = setup_pair(tmp_path)
    monkeypatch.setattr(module, '_conflict_name', lambda rel: 'a (conflict fixed)')
    remote = fake.ensure_folder_path('Backup')
    fid = fake.add_file('a', b'base', remote)
    engine = TwoWaySyncEngine(fake, db, pair)
    engine.run()
    for value in [b'local-one', b'local-two']:
        (root / 'a').write_bytes(value)
        fake.modify_file(fid, b'remote-' + value, modified='2099-01-01T00:00:00Z')
        fake.nodes[fid]['modified'] = '2099-01-01T00:00:00Z'
        assert engine.run().conflicts == 1
    backups = [p.read_bytes() for p in root.glob('a (conflict fixed)*')]
    assert sorted(backups) == [b'local-one', b'local-two']


def test_remote_size_exclusion_does_not_become_local_deletion(tmp_path):
    root, fake, db, pair = setup_pair(tmp_path)
    remote = fake.ensure_folder_path('Backup')
    fid = fake.add_file('a', b'old', remote)
    TwoWaySyncEngine(fake, db, pair).run()
    fake.modify_file(fid, b'remote too large')
    report = TwoWaySyncEngine(fake, db, pair, SyncRules(max_file_size_bytes=5)).run()
    assert report.downloaded == report.trashed_local == report.trashed_remote == 0
    assert (root / 'a').read_bytes() == b'old'


def test_replaced_remote_file_id_is_not_treated_as_local_deletion(tmp_path):
    root, fake, db, pair = setup_pair(tmp_path)
    remote = fake.ensure_folder_path('Backup')
    fid = fake.add_file('a', b'old', remote)
    engine = TwoWaySyncEngine(fake, db, pair)
    engine.run()
    (root / 'a').unlink()
    fake.trash(fid)
    replacement = fake.add_file('a', b'old', remote)
    report = engine.run()
    assert report.trashed_remote == 0
    assert report.downloaded == 1
    assert not fake.nodes[replacement]['trashed']


def test_excluded_directory_suffix_is_consistent_on_both_sides(tmp_path):
    root, fake, db, pair = setup_pair(tmp_path)
    (root / 'skip.tmp').mkdir()
    (root / 'skip.tmp' / 'a').write_bytes(b'keep')
    report = TwoWaySyncEngine(fake, db, pair).run()
    assert report.uploaded == 0


def test_symlink_target_excluded_from_existing_history(tmp_path):
    root, fake, db, pair = setup_pair(tmp_path)
    (root / 'a').write_bytes(b'old')
    engine = TwoWaySyncEngine(fake, db, pair)
    engine.run()
    (root / 'a').unlink()
    outside = tmp_path / 'outside'
    outside.write_bytes(b'secret')
    (root / 'a').symlink_to(outside)
    report = engine.run()
    assert report.trashed_remote == report.uploaded == report.downloaded == 0
    assert outside.read_bytes() == b'secret'


def test_upload_reads_pinned_source_when_file_changes_to_symlink(tmp_path):
    root, fake, db, pair = setup_pair(tmp_path)
    source = root / 'a'
    source.write_bytes(b'intended')
    outside = tmp_path / 'secret'
    outside.write_bytes(b'PRIVATE')
    original = fake.upload_file
    def replace_before_read(*args, **kwargs):
        source.unlink()
        source.symlink_to(outside)
        return original(*args, **kwargs)
    fake.upload_file = replace_before_read
    report = TwoWaySyncEngine(fake, db, pair).run()
    assert report.uploaded == 1
    fid = fake.uploads[0][-1]
    assert fake.nodes[fid]['content'] == b'intended'
    assert outside.read_bytes() == b'PRIVATE'


def test_upload_only_pins_source_against_symlink_swap(tmp_path):
    root, fake, db, pair = setup_pair(tmp_path)
    source = root / 'a'
    source.write_bytes(b'intended')
    secret = tmp_path / 'secret'
    secret.write_bytes(b'PRIVATE')
    upload = fake.upload_file
    def swap(*args, **kwargs):
        source.unlink()
        source.symlink_to(secret)
        return upload(*args, **kwargs)
    fake.upload_file = swap
    result = UploadOnlySyncEngine(drive_client=fake, database=db).sync_up(root, 'Backup')
    assert result.uploaded == 1
    assert fake.nodes[fake.uploads[0][-1]]['content'] == b'intended'


def test_unreadable_local_subtree_aborts_before_remote_deletions(tmp_path, monkeypatch):
    import os
    root, fake, db, pair = setup_pair(tmp_path)
    (root / 'sub').mkdir()
    (root / 'sub' / 'a').write_bytes(b'keep')
    engine = TwoWaySyncEngine(fake, db, pair)
    engine.run()
    original = os.scandir
    def unreadable(path):
        if isinstance(path, (str, os.PathLike)) and Path(path) == root / 'sub':
            raise PermissionError('unmounted or inaccessible subtree')
        return original(path)
    monkeypatch.setattr(os, 'scandir', unreadable)
    assert TwoWaySyncEngine(fake, db, pair).run().failed > 0
    assert not any(node['trashed'] for node in fake.nodes.values())


def test_edit_during_remote_download_is_preserved_in_recovery_backup(tmp_path):
    root, fake, db, pair = setup_pair(tmp_path)
    remote = fake.ensure_folder_path('Backup')
    fid = fake.add_file('a', b'old', remote)
    TwoWaySyncEngine(fake, db, pair).run()
    fake.modify_file(fid, b'new remote')
    download = fake.download_file
    def edit_during_download(*args, **kwargs):
        (root / 'a').write_bytes(b'UNIQUE USER EDIT')
        return download(*args, **kwargs)
    fake.download_file = edit_during_download
    report = TwoWaySyncEngine(fake, db, pair).run()
    assert report.downloaded == 1
    assert (root / 'a').read_bytes() == b'new remote'
    recovered = [p.read_bytes() for p in (root / '.electridrive-trash').rglob('*') if p.is_file()]
    assert b'UNIQUE USER EDIT' in recovered


def test_file_created_during_initial_download_is_preserved(tmp_path):
    root, fake, db, pair = setup_pair(tmp_path)
    remote = fake.ensure_folder_path('Backup')
    fake.add_file('a', b'remote', remote)
    download = fake.download_file
    def create_during_download(*args, **kwargs):
        (root / 'a').write_bytes(b'UNIQUE NEW FILE')
        return download(*args, **kwargs)
    fake.download_file = create_during_download
    report = TwoWaySyncEngine(fake, db, pair).run()
    assert report.downloaded == 1
    recovered = [p.read_bytes() for p in (root / '.electridrive-trash').rglob('*') if p.is_file()]
    assert b'UNIQUE NEW FILE' in recovered


def test_download_publish_never_clobbers_file_created_at_commit(tmp_path, monkeypatch):
    import os
    root, fake, db, pair = setup_pair(tmp_path)
    remote = fake.ensure_folder_path('Backup')
    fake.add_file('a', b'remote', remote)
    original = os.link
    def concurrent_creation(*args, **kwargs):
        if kwargs.get('dst_dir_fd') is not None:
            (root / 'a').write_bytes(b'CREATED AT COMMIT')
        return original(*args, **kwargs)
    monkeypatch.setattr(os, 'link', concurrent_creation)
    report = TwoWaySyncEngine(fake, db, pair).run()
    assert (root / 'a').read_bytes() == b'CREATED AT COMMIT'
    assert report.failed >= 1


def test_configured_runner_stops_after_cancellation(tmp_path):
    from electridrive.sync.runner import run_configured_pairs
    root, fake, db, pair = setup_pair(tmp_path)
    (root / 'a').write_bytes(b'first')
    second = tmp_path / 'second'
    second.mkdir()
    (second / 'b').write_bytes(b'never upload')
    event = threading.Event()
    original = fake.upload_file
    def cancel(*args, **kwargs):
        result = original(*args, **kwargs)
        event.set()
        return result
    fake.upload_file = cancel
    with pytest.raises(RuntimeError, match='[Cc]ancel'):
        run_configured_pairs(fake, db, [pair, SyncPair(str(second), 'Second')], cancel_event=event)
    assert fake.find_folder('Second', 'root') is None


def test_upload_only_cancel_during_upload_stops_later_files(tmp_path):
    root, fake, db, pair = setup_pair(tmp_path)
    (root / 'a').write_bytes(b'first')
    (root / 'z').write_bytes(b'later')
    event = threading.Event()
    original = fake.upload_file
    def cancel(*args, **kwargs):
        event.set()
        return original(*args, **kwargs)
    fake.upload_file = cancel
    with pytest.raises(RuntimeError, match='[Cc]ancel'):
        UploadOnlySyncEngine(drive_client=fake, database=db, cancel_event=event).sync_up(root, 'Backup')
    assert len(fake.uploads) == 1


def test_lock_excludes_independent_process(tmp_path):
    import os
    import subprocess
    import sys
    from electridrive.sync.safety import pair_lock
    root = tmp_path / 'root'
    root.mkdir()
    code = '''from pathlib import Path
from electridrive.sync.safety import pair_lock
import sys
try:
    with pair_lock(Path(sys.argv[1])):
        sys.exit(9)
except RuntimeError:
    sys.exit(0)
'''
    with pair_lock(root):
        result = subprocess.run([sys.executable, '-c', code, str(root)], env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'), capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_explicit_remote_root_rebind_does_not_reuse_deletion_history(tmp_path):
    root, fake, db, pair = setup_pair(tmp_path)
    (root / 'a').write_bytes(b'keep')
    engine = TwoWaySyncEngine(fake, db, pair)
    engine.run()
    fake.trash(fake.ensure_folder_path('Backup'))
    db.set_meta(engine._binding_key, '')  # Explicit fresh relationship.
    replacement = fake.add_folder('Backup')
    report = TwoWaySyncEngine(fake, db, pair).run()
    assert report.trashed_local == 0
    assert (root / 'a').read_bytes() == b'keep'
    assert fake.find_file('a', replacement)


def test_manual_download_plan_preserves_existing_destination(tmp_path):
    fake = FakeDrive()
    fid = fake.add_file('existing.txt', b'remote')
    target = tmp_path / 'existing.txt'
    target.write_bytes(b'LOCAL')
    plan = plan_download(fake, fake.get_metadata(fid), tmp_path)
    assert plan[0].dest_path != target
    assert target.read_bytes() == b'LOCAL'


def test_upload_only_checks_cancellation_at_media_progress(tmp_path):
    root, fake, db, pair = setup_pair(tmp_path)
    (root / 'a').write_bytes(b'content')
    event = threading.Event()
    completed = []
    def upload(path, parent, name, progress_cb=None):
        event.set()
        if progress_cb:
            progress_cb(1, 10)
        completed.append('remaining upload chunks')
        return fake.add_file(name, b'content', parent)
    fake.upload_file = upload
    with pytest.raises(RuntimeError, match='[Cc]ancel'):
        UploadOnlySyncEngine(drive_client=fake, database=db, cancel_event=event).sync_up(root, 'Backup')
    assert completed == []


@pytest.mark.parametrize('replace_root', [False, True])
def test_explicit_local_relationship_reset_does_not_reuse_deletion_history(tmp_path, replace_root):
    root, fake, db, pair = setup_pair(tmp_path)
    (root / 'a').write_bytes(b'keep remote')
    old_engine = TwoWaySyncEngine(fake, db, pair)
    assert old_engine.run().uploaded == 1
    if replace_root:
        root.rename(tmp_path / 'old-root')
        root.mkdir()
    else:
        (root / 'a').unlink()
    db.set_meta(old_engine._binding_key, '')
    engine = TwoWaySyncEngine(fake, db, pair)
    report = engine.run()
    assert report.trashed_remote == 0
    assert report.downloaded == 1
    assert (root / 'a').read_bytes() == b'keep remote'
    assert engine.pair_id != old_engine.pair_id
    # The new generation must persist across ordinary restarts.
    restarted = TwoWaySyncEngine(fake, db, pair)
    assert restarted.run().failed == 0
    assert restarted.pair_id == engine.pair_id


def test_legacy_generationless_binding_starts_non_destructive_baseline(tmp_path):
    import json
    root, fake, db, pair = setup_pair(tmp_path)
    (root / 'a').write_bytes(b'keep remote')
    engine = TwoWaySyncEngine(fake, db, pair)
    assert engine.run().uploaded == 1
    binding = json.loads(db.get_meta(engine._binding_key))
    binding.pop('generation', None)
    db.set_meta(engine._binding_key, json.dumps(binding))
    (root / 'a').unlink()
    restarted = TwoWaySyncEngine(fake, db, pair)
    report = restarted.run()
    assert report.trashed_remote == 0
    assert report.downloaded == 1
    assert (root / 'a').read_bytes() == b'keep remote'
    assert restarted.pair_id != engine.pair_id
