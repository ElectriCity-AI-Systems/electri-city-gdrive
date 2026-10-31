"""Conservative Changes API gating: any change still requires a full inventory."""
from pathlib import Path
import pytest

from electridrive.config import SyncPair
from electridrive.google_api.client import RemoteChange
from electridrive.storage.database import SyncDatabase
from electridrive.sync.rules import SyncRules
from electridrive.sync.twoway import TwoWaySyncEngine
from fakes import FakeDrive


def environment(tmp_path):
    root = tmp_path / 'local'
    root.mkdir()
    drive = FakeDrive()
    folder = drive.ensure_folder_path('Backup')
    drive.add_file('a', b'content', folder)
    pair = SyncPair(local_path=str(root), remote_folder='Backup', include_all_files=False)
    db = SyncDatabase(tmp_path / 'state.db')
    return root, drive, folder, pair, db


def test_unchanged_remote_reuses_persisted_snapshot_without_folder_listing(tmp_path):
    root, drive, folder, pair, db = environment(tmp_path)
    assert TwoWaySyncEngine(drive, db, pair).run().downloaded == 1
    def forbidden(*args):
        pytest.fail('No-change cycle must reuse the persisted inventory')
    drive.list_folder = forbidden
    assert TwoWaySyncEngine(drive, db, pair).run().failed == 0
    assert (root / 'a').read_bytes() == b'content'


def test_remote_change_invalidates_snapshot_and_triggers_full_scan(tmp_path):
    root, drive, folder, pair, db = environment(tmp_path)
    TwoWaySyncEngine(drive, db, pair).run()
    drive.add_file('b', b'new', folder)
    assert TwoWaySyncEngine(drive, db, pair).run().downloaded == 1
    assert (root / 'b').read_bytes() == b'new'


@pytest.mark.parametrize('failure', [OSError('network failed'), ValueError('410 expired token')])
def test_change_feed_failure_falls_back_to_complete_inventory(tmp_path, failure):
    root, drive, folder, pair, db = environment(tmp_path)
    TwoWaySyncEngine(drive, db, pair).run()
    drive.add_file('b', b'new', folder)
    def broken(token):
        raise failure
    drive.list_changes = broken
    assert TwoWaySyncEngine(drive, db, pair).run().downloaded == 1
    assert (root / 'b').read_bytes() == b'new'


def test_changes_during_full_scan_are_rescanned_before_current_actions(tmp_path):
    root, drive, folder, pair, db = environment(tmp_path)
    original = drive.list_folder
    token = {'generation': 0}
    drive.get_start_page_token = lambda: str(token['generation'])
    def changes(previous):
        changed = previous != str(token['generation'])
        return ([RemoteChange('late', False)] if changed else []), None, str(token['generation'])
    drive.list_changes = changes
    def during_scan(parent, page=None):
        result = original(parent, page)
        if token['generation'] == 0:
            drive.add_file('late', b'created during scan', folder)
            token['generation'] = 1
        return result
    drive.list_folder = during_scan
    assert TwoWaySyncEngine(drive, db, pair).run().downloaded == 2
    drive.list_folder = original
    assert TwoWaySyncEngine(drive, db, pair).run().downloaded == 0
    assert (root / 'late').read_bytes() == b'created during scan'


def test_rule_change_forces_full_scan(tmp_path):
    root, drive, folder, pair, db = environment(tmp_path)
    drive.add_file('.secret', b'secret', folder)
    TwoWaySyncEngine(drive, db, pair).run()
    assert TwoWaySyncEngine(drive, db, pair, SyncRules(include_hidden=True)).run().downloaded == 1


def test_cached_inventory_does_not_hide_previously_symlink_excluded_file(tmp_path):
    root, drive, folder, pair, db = environment(tmp_path)
    outside = tmp_path / 'outside'
    outside.write_bytes(b'private')
    (root / 'a').symlink_to(outside)
    assert TwoWaySyncEngine(drive, db, pair).run().downloaded == 0
    (root / 'a').unlink()
    assert TwoWaySyncEngine(drive, db, pair).run().downloaded == 1


def test_partial_fallback_inventory_aborts_without_deletion_plan(tmp_path):
    root, drive, folder, pair, db = environment(tmp_path)
    TwoWaySyncEngine(drive, db, pair).run()
    drive.list_changes = lambda token: ([RemoteChange('a', False)], None, 'new')
    def broken(*args):
        raise OSError('inventory interrupted')
    drive.list_folder = broken
    assert TwoWaySyncEngine(drive, db, pair).run().failed > 0
    assert (root / 'a').read_bytes() == b'content'


def test_paginated_empty_change_feed_avoids_full_listing(tmp_path):
    root, drive, folder, pair, db = environment(tmp_path)
    TwoWaySyncEngine(drive, db, pair).run()
    visited = []
    def pages(token):
        visited.append(token)
        if token == 'second-page':
            return [], None, 'final-token'
        return [], 'second-page', None
    drive.list_changes = pages
    def forbidden(*args):
        pytest.fail('All empty change pages should permit snapshot reuse')
    drive.list_folder = forbidden
    assert TwoWaySyncEngine(drive, db, pair).run().failed == 0
    assert len(visited) == 2
    assert visited[-1] == 'second-page'


def test_change_on_later_feed_page_forces_complete_inventory(tmp_path):
    root, drive, folder, pair, db = environment(tmp_path)
    TwoWaySyncEngine(drive, db, pair).run()
    previous_token = drive.get_start_page_token()
    drive.add_file('b', b'new', folder)
    def pages(token):
        if token == 'second-page':
            return [RemoteChange('b', False)], None, 'final-token'
        if token == previous_token:
            return [], 'second-page', None
        return [], None, token
    drive.list_changes = pages
    assert TwoWaySyncEngine(drive, db, pair).run().downloaded == 1


def test_corrupt_persisted_snapshot_requires_full_inventory(tmp_path):
    import json
    root, drive, folder, pair, db = environment(tmp_path)
    engine = TwoWaySyncEngine(drive, db, pair)
    engine.run()
    saved = json.loads(db.get_meta(engine._snapshot_key))
    saved['snapshot']['entries'] = []  # Incomplete state must not imply deletion.
    db.set_meta(engine._snapshot_key, json.dumps(saved))
    listings = []
    original = drive.list_folder
    def list_folder(*args):
        listings.append(args)
        return original(*args)
    drive.list_folder = list_folder
    report = TwoWaySyncEngine(drive, db, pair).run()
    assert listings
    assert report.trashed_local == report.trashed_remote == 0
    assert (root / 'a').read_bytes() == b'content'


def test_start_token_failure_does_not_cache_untracked_inventory(tmp_path):
    root, drive, folder, pair, db = environment(tmp_path)
    def broken():
        raise OSError('token service unavailable')
    drive.get_start_page_token = broken
    assert TwoWaySyncEngine(drive, db, pair).run().downloaded == 1
    drive.add_file('b', b'new', folder)
    assert TwoWaySyncEngine(drive, db, pair).run().downloaded == 1


def test_own_remote_mutation_invalidates_pre_action_snapshot(tmp_path):
    root, drive, folder, pair, db = environment(tmp_path)
    (root / 'b').write_bytes(b'local upload')
    first = TwoWaySyncEngine(drive, db, pair).run()
    assert first.uploaded == first.downloaded == 1
    # Even if a provider temporarily reports no feed changes, the inventory
    # captured before our upload must not classify that upload as remote deletion.
    drive.list_changes = lambda token: ([], None, token)
    listings = []
    original = drive.list_folder
    def counted(*args):
        listings.append(args)
        return original(*args)
    drive.list_folder = counted
    report = TwoWaySyncEngine(drive, db, pair).run()
    assert listings
    assert report.trashed_local == report.trashed_remote == 0
    assert (root / 'b').read_bytes() == b'local upload'


def test_remote_edit_during_inventory_is_preserved_before_local_upload(tmp_path):
    root, drive, folder, pair, db = environment(tmp_path)
    engine = TwoWaySyncEngine(drive, db, pair)
    assert engine.run().downloaded == 1
    fid = drive.find_file('a', folder)
    (root / 'a').write_bytes(b'LOCAL EDIT')
    drive.add_file('force-inventory', b'unrelated', folder)
    listing = drive.list_folder
    edited = False

    def concurrent_edit(parent, page=None):
        nonlocal edited
        result = listing(parent, page)
        if not edited:
            edited = True
            drive.modify_file(fid, b'UNIQUE REMOTE EDIT', modified='2099-01-01T00:00:00Z')
        return result

    drive.list_folder = concurrent_edit
    report = engine.run()
    assert report.failed == 0
    assert report.conflicts == 1
    assert drive.nodes[fid]['content'] == b'UNIQUE REMOTE EDIT'
    contents = [path.read_bytes() for path in root.rglob('*') if path.is_file()]
    assert b'LOCAL EDIT' in contents
    assert b'UNIQUE REMOTE EDIT' in contents


def test_continuously_changing_inventory_aborts_before_mutations(tmp_path):
    root, drive, folder, pair, db = environment(tmp_path)
    engine = TwoWaySyncEngine(drive, db, pair)
    assert engine.run().downloaded == 1
    fid = drive.find_file('a', folder)
    (root / 'a').write_bytes(b'LOCAL EDIT')
    drive.add_file('force-inventory', b'unrelated', folder)
    listing = drive.list_folder
    scans = []

    def concurrent_edit(parent, page=None):
        result = listing(parent, page)
        scans.append(parent)
        drive.modify_file(fid, b'UNIQUE REMOTE EDIT', modified='2099-01-01T00:00:00Z')
        return result

    drive.list_folder = concurrent_edit
    report = engine.run()
    assert report.failed > 0 and report.blocked_before_transfers
    assert report.uploaded == report.downloaded == report.trashed_remote == 0
    assert (root / 'a').read_bytes() == b'LOCAL EDIT'
    assert drive.nodes[fid]['content'] == b'UNIQUE REMOTE EDIT'
    assert 1 <= len(scans) <= 3
