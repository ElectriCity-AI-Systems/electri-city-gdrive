import os
import stat
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from electridrive.cli import build_parser, cmd_mount
from electridrive.config import Settings
from electridrive.vfs import DriveTree, FuseMount, fuse_available
from electridrive.vfs.fuse_mount import create_operations_class
from fakes import FakeDrive


def _tree(tmp_path: Path):
    fake = FakeDrive()
    projects = fake.add_folder("Projects")
    fake.add_file("a.txt", b"hello-a", parent=projects)
    fake.add_file("Memo", b"memo", mime="application/vnd.google-apps.document")
    return fake, DriveTree(fake, "root", tmp_path / "cache")


def test_resolve_and_list(tmp_path: Path):
    _fake, tree = _tree(tmp_path)
    assert tree.resolve("/") is None  # root is implicit
    names = {f.name for f in tree.list_dir("/")}
    assert {"Projects", "Memo"} <= names
    node = tree.resolve("/Projects/a.txt")
    assert node is not None and node.name == "a.txt"
    assert tree.resolve("/Projects/missing") is None


def test_ensure_cached_reuses_unchanged_download(tmp_path: Path, monkeypatch):
    fake, tree = _tree(tmp_path)
    node = tree.resolve("/Projects/a.txt")
    downloads = 0
    original_download = fake.download_file

    def record_download(*args, **kwargs):
        nonlocal downloads
        downloads += 1
        return original_download(*args, **kwargs)

    monkeypatch.setattr(fake, "download_file", record_download)
    path = tree.ensure_cached(node)
    assert path.read_bytes() == b"hello-a"
    tree.invalidate()
    assert tree.ensure_cached(tree.resolve("/Projects/a.txt")) == path
    assert downloads == 1


def test_ensure_cached_redownloads_after_remote_md5_change(tmp_path: Path, monkeypatch):
    fake, tree = _tree(tmp_path)
    node = tree.resolve("/Projects/a.txt")
    downloads = 0
    original_download = fake.download_file

    def record_download(*args, **kwargs):
        nonlocal downloads
        downloads += 1
        return original_download(*args, **kwargs)

    monkeypatch.setattr(fake, "download_file", record_download)
    path = tree.ensure_cached(node)

    # Keep modifiedTime fixed so this specifically exercises md5Checksum identity.
    fake.nodes[node.id]["content"] = b"changed remotely"
    tree.ttl = 0
    changed_node = tree.resolve("/Projects/a.txt")

    assert tree.ensure_cached(changed_node) == path
    assert path.read_bytes() == b"changed remotely"
    assert downloads == 2


def test_stale_cached_size_is_not_used_for_changed_remote(tmp_path: Path, monkeypatch):
    class FakeFuseOSError(OSError):
        pass

    monkeypatch.setitem(
        sys.modules,
        "fuse",
        SimpleNamespace(FuseOSError=FakeFuseOSError, Operations=object),
    )
    fake, tree = _tree(tmp_path)
    node = tree.resolve("/Projects/a.txt")
    tree.ensure_cached(node)

    replacement = b"a much longer remote payload"
    fake.nodes[node.id]["content"] = replacement
    tree.ttl = 0

    fs = create_operations_class()(tree)
    assert fs.getattr("/Projects/a.txt")["st_size"] == len(replacement)


def test_failed_cache_refresh_preserves_last_complete_download(tmp_path: Path, monkeypatch):
    fake, tree = _tree(tmp_path)
    node = tree.resolve("/Projects/a.txt")
    path = tree.ensure_cached(node)

    fake.nodes[node.id]["content"] = b"changed remotely"
    tree.ttl = 0
    changed_node = tree.resolve("/Projects/a.txt")

    def interrupted_download(_file_id, dest_path, progress_cb=None):
        Path(dest_path).write_bytes(b"partial")
        raise OSError("network interrupted")

    monkeypatch.setattr(fake, "download_file", interrupted_download)
    with pytest.raises(OSError, match="network interrupted"):
        tree.ensure_cached(changed_node)

    assert path.read_bytes() == b"hello-a"
    assert not list((tmp_path / "cache").glob(".*.tmp"))


def test_cache_without_remote_identity_is_not_trusted_indefinitely(tmp_path: Path, monkeypatch):
    fake, tree = _tree(tmp_path)
    node = replace(
        tree.resolve("/Projects/a.txt"),
        md5_checksum=None,
        modified_time=None,
    )
    downloads = 0
    original_download = fake.download_file

    def record_download(*args, **kwargs):
        nonlocal downloads
        downloads += 1
        return original_download(*args, **kwargs)

    monkeypatch.setattr(fake, "download_file", record_download)
    tree.ensure_cached(node)
    tree.ensure_cached(node)

    assert downloads == 2


def test_workspace_cache_reuses_then_reexports_after_modified_time_change(
    tmp_path: Path, monkeypatch
):
    fake, tree = _tree(tmp_path)
    # Workspace files do not receive md5Checksum from the real Drive API.
    node = replace(tree.resolve("/Memo"), md5_checksum=None)
    exports = 0
    original_export = fake.export_file

    def record_export(*args, **kwargs):
        nonlocal exports
        exports += 1
        return original_export(*args, **kwargs)

    monkeypatch.setattr(fake, "export_file", record_export)
    path = tree.ensure_cached(node)
    assert path.read_bytes() == b"EXPORTED:memo"
    tree.invalidate()
    unchanged_node = replace(tree.resolve("/Memo"), md5_checksum=None)
    assert tree.ensure_cached(unchanged_node) == path
    assert exports == 1

    fake.nodes[node.id]["content"] = b"revised memo"
    fake.nodes[node.id]["modified"] = "2024-02-01T00:00:00Z"
    tree.ttl = 0
    changed_node = replace(tree.resolve("/Memo"), md5_checksum=None)

    assert tree.ensure_cached(changed_node) == path
    assert path.read_bytes() == b"EXPORTED:revised memo"
    assert exports == 2


def test_cache_invalidate_reflects_new_files(tmp_path: Path):
    fake, tree = _tree(tmp_path)
    assert "later.txt" not in {f.name for f in tree.list_dir("/")}
    fake.add_file("later.txt", b"x")
    tree.invalidate()
    assert "later.txt" in {f.name for f in tree.list_dir("/")}


def test_fuse_available_returns_bool():
    assert isinstance(fuse_available(), bool)


def test_fuse_operations_are_always_read_only(tmp_path: Path, monkeypatch):
    class FakeFuseOSError(OSError):
        pass

    monkeypatch.setitem(
        sys.modules,
        "fuse",
        SimpleNamespace(FuseOSError=FakeFuseOSError, Operations=object),
    )
    _fake, tree = _tree(tmp_path)
    fs_class = create_operations_class()

    with pytest.raises(ValueError, match="read-only in version 2.1.0"):
        fs_class(tree, writable=True)

    fs = fs_class(tree)
    mode = fs.getattr("/Projects/a.txt")["st_mode"]
    assert stat.S_ISREG(mode)
    assert mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH) == 0
    root_mode = fs.getattr("/")["st_mode"]
    folder_mode = fs.getattr("/Projects")["st_mode"]
    assert root_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH) == 0
    assert folder_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH) == 0
    with pytest.raises(FakeFuseOSError):
        fs.access("/Projects/a.txt", os.W_OK)
    with pytest.raises(FakeFuseOSError):
        fs.open("/Projects/a.txt", os.O_WRONLY)


def test_writable_mount_is_rejected_before_mount_side_effects(tmp_path: Path, monkeypatch):
    mountpoint = tmp_path / "mountpoint"
    mount = FuseMount(object(), tmp_path / "cache")

    def fuse_must_not_be_checked():
        pytest.fail("writable mode must fail before checking or starting FUSE")

    monkeypatch.setattr("electridrive.vfs.fuse_mount.fuse_available", fuse_must_not_be_checked)
    with pytest.raises(ValueError, match="read-only in version 2.1.0"):
        mount.start(str(mountpoint), writable=True)
    assert not mountpoint.exists()
    assert not mount.is_mounted


def test_cli_hides_and_rejects_legacy_writable_option(capsys):
    parser = build_parser()
    subparsers = next(action for action in parser._actions if action.dest == "command")
    mount_parser = subparsers.choices["mount"]
    assert "--writable" not in mount_parser.format_help()

    args = parser.parse_args(["mount", "/unused", "--writable"])
    assert cmd_mount(args) == 2
    assert "Virtual Drive is read-only" in capsys.readouterr().out


def test_legacy_writable_setting_is_ignored():
    settings = Settings.from_dict({"mountpoint": "/safe", "vfs_writable": True})
    assert settings.mountpoint == "/safe"
    assert not hasattr(settings, "vfs_writable")
    assert "vfs_writable" not in settings.to_dict()
