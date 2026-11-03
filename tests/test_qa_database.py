"""QA database persistence never opens SQLite pathnames or sidecars."""
import json
import pytest
from electridrive import qa_policy as qa
from electridrive.config import get_paths
from electridrive.storage.database import SyncDatabase

@pytest.fixture
def policy(monkeypatch, tmp_path):
    monkeypatch.setattr(qa, '_active', None)
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path/'ordinary-config'))
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path/'ordinary-state'))
    p = qa.initialize(tmp_path/'profile', tmp_path/'synthetic')
    monkeypatch.setenv('ELECTRIDRIVE_QA_PROFILE', str(p.profile))
    return qa.current_policy()

def test_database_symlink_after_paths_never_opens_outside(policy, tmp_path):
    paths = get_paths()
    outside = tmp_path/'outside.sqlite'
    paths.database_file.symlink_to(outside)
    db = SyncDatabase(paths.database_file)
    db.set_change_token('synthetic', 'token')
    db.close()
    assert not outside.exists()

def test_snapshot_persists_metadata(policy):
    path = get_paths().database_file
    db = SyncDatabase(path)
    db.set_change_token('synthetic', 'token')
    db.close()
    db = SyncDatabase(path)
    assert db.get_change_token('synthetic') == 'token'
    db.close()
    assert not path.exists()

@pytest.mark.parametrize('name', ['qa-sync-state.json', 'qa-sync-state.lock'])
def test_symlink_state_files_refused(policy, tmp_path, name):
    path = get_paths().database_file
    (path.parent/name).symlink_to(tmp_path/'outside')
    with pytest.raises(qa.QaSafetyError):
        SyncDatabase(path)

def test_malformed_snapshot_refused(policy):
    path = get_paths().database_file
    qa._write(path.parent/'qa-sync-state.json', {'version':1,'tables':{'arbitrary':[]}})
    with pytest.raises(qa.QaSafetyError):
        SyncDatabase(path)

def test_concurrent_database_refused(policy):
    path = get_paths().database_file
    db = SyncDatabase(path)
    try:
        with pytest.raises(qa.QaSafetyError):
            SyncDatabase(path)
    finally:
        db.close()

@pytest.mark.parametrize('name', ['qa-sync-state.json', 'qa-sync-state.lock'])
def test_swapped_state_files_stop_existing_database(policy, tmp_path, name):
    path = get_paths().database_file
    db = SyncDatabase(path)
    target = path.parent/name
    target.unlink()
    target.symlink_to(tmp_path/'outside')
    try:
        with pytest.raises(qa.QaSafetyError):
            db.stats()
    finally:
        with pytest.raises(qa.QaSafetyError):
            db.close()
    assert not (tmp_path/'outside').exists()

def test_transfers_preserve_autoincrement_after_reopen(policy):
    path = get_paths().database_file
    db = SyncDatabase(path)
    first = db.add_transfer(kind='upload', name='synthetic')
    db.close()
    db = SyncDatabase(path)
    assert db.add_transfer(kind='upload', name='next') == first + 1
    assert len(db.list_transfers()) == 2
    db.close()


def test_lock_open_rejects_swapped_symlink_ancestor(policy, tmp_path, monkeypatch):
    import os
    path = get_paths().database_file
    state = policy.profile/'state'
    outside = tmp_path/'outside'
    (outside/'electridrive').mkdir(parents=True)
    original = qa._no_links
    swapped = False
    def swap_after_validation(name):
        nonlocal swapped
        original(name)
        if not swapped and name == path.parent:
            swapped = True
            state.rename(policy.profile/'state-saved')
            state.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(qa, '_no_links', swap_after_validation)
    with pytest.raises((qa.QaSafetyError, OSError)):
        SyncDatabase(path)
    assert not (outside/'electridrive/qa-sync-state.lock').exists()
