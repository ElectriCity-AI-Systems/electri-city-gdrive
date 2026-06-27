from pathlib import Path

import pytest

from electridrive.sync.rules import SyncRules, assert_no_delete_allowed


def test_default_excludes_git():
    rules = SyncRules()
    assert rules.is_excluded(Path('/tmp/project/.git/config'), Path('/tmp/project'))


def test_default_excludes_hidden():
    rules = SyncRules()
    assert rules.is_excluded(Path('/tmp/project/.secret'), Path('/tmp/project'))


def test_can_include_hidden_when_enabled():
    rules = SyncRules(include_hidden=True)
    assert not rules.is_excluded(Path('/tmp/project/.secret'), Path('/tmp/project'))


def test_default_excludes_sqlite_sidecars():
    rules = SyncRules()
    root = Path('/tmp/project')
    assert rules.is_excluded(root / 'state.db-journal', root)
    assert rules.is_excluded(root / 'state.db-wal', root)
    assert rules.is_excluded(root / 'state.db-shm', root)


def test_delete_blocked():
    with pytest.raises(PermissionError):
        assert_no_delete_allowed()
