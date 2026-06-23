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


def test_delete_blocked():
    with pytest.raises(PermissionError):
        assert_no_delete_allowed()
