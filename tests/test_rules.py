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


def test_full_backup_mode_has_no_name_hidden_suffix_or_size_exclusions():
    rules = SyncRules.full_backup()
    root = Path("/tmp/project")
    for name in (
        ".git/config", ".env", ".venv/bin/python", "node_modules/mod.js",
        ".electridrive-trash/old.bak~", "tmp/cache.part", "credentials.json",
        ".secret", "bigfile.bin",
    ):
        assert not rules.is_excluded_relative(name, is_file=True, size=10**12)
    assert rules.fail_on_unsupported


def test_legacy_filtered_mode_remains_available():
    rules = SyncRules()
    assert rules.is_excluded_relative(".git/config", is_file=True)
    assert rules.is_excluded_relative("file.tmp", is_file=True)
