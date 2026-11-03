from pathlib import Path
import subprocess
import threading

import pytest

from electridrive.vfs.fuse_mount import DriveTree, FuseMount
from fakes import FakeDrive


def test_cache_refreshes_changed_remote_and_corrupted_bytes(tmp_path):
    drive = FakeDrive()
    fid = drive.add_file('a', b'old')
    tree = DriveTree(drive, 'root', tmp_path)
    path = tree.ensure_cached(tree.resolve('/a'))
    drive.nodes[fid]['content'] = b'new'
    tree.invalidate()
    assert tree.ensure_cached(tree.resolve('/a')).read_bytes() == b'new'
    path.write_bytes(b'bad')
    assert tree.ensure_cached(tree.resolve('/a')).read_bytes() == b'new'


def test_failed_cache_download_never_publishes_partial(tmp_path):
    drive = FakeDrive()
    drive.add_file('a', b'complete')
    tree = DriveTree(drive, 'root', tmp_path)
    node = tree.resolve('/a')
    good = drive.download_file
    def fail(fid, dest, progress_cb=None):
        Path(dest).write_bytes(b'PART')
        raise OSError('offline')
    drive.download_file = fail
    with pytest.raises(OSError):
        tree.ensure_cached(node)
    drive.download_file = good
    assert tree.ensure_cached(node).read_bytes() == b'complete'


def test_duplicate_and_unsafe_mount_names_preserve_all_ids(tmp_path):
    drive = FakeDrive()
    ids = {drive.add_file(name) for name in ('same', 'same', 'a/b', 'a_b', '..')}
    tree = DriveTree(drive, 'root', tmp_path)
    names = tree.children('root')
    assert len(names) == 5
    assert all('/' not in name and name not in ('.', '..') for name in names)
    assert {tree.resolve('/' + name).id for name in names} == ids
    tree.invalidate()
    assert list(tree.children('root')) == list(names)


def test_unmount_failure_retains_live_mount(tmp_path, monkeypatch):
    mount = FuseMount(FakeDrive(), tmp_path)
    class Live:
        def is_alive(self): return True
        def join(self, timeout=None): pass
    thread = Live()
    mount._thread = thread
    mount.mountpoint = '/fake'
    monkeypatch.setattr('electridrive.vfs.fuse_mount.shutil.which', lambda _: '/helper')
    monkeypatch.setattr('electridrive.vfs.fuse_mount.subprocess.run',
                        lambda *a, **kw: subprocess.CompletedProcess(a, 1, stderr='busy'))
    with pytest.raises(RuntimeError, match='busy'):
        mount.stop()
    assert mount.is_mounted and mount._thread is thread and mount.mountpoint == '/fake'


def test_writable_mount_is_rejected_before_mounting(tmp_path):
    with pytest.raises(ValueError, match='read-only'):
        FuseMount(FakeDrive(), tmp_path).start('/unused', writable=True)


def test_cache_integrity_is_streamed_without_reading_whole_file(tmp_path, monkeypatch):
    drive = FakeDrive()
    drive.add_file('large', b'a' * 1024)
    tree = DriveTree(drive, 'root', tmp_path)
    node = tree.resolve('/large')
    def unbounded_read(_path):
        raise AssertionError('unbounded whole-file read')
    monkeypatch.setattr(Path, 'read_bytes', unbounded_read)
    path = tree.ensure_cached(node)
    assert tree.ensure_cached(node) == path


def test_fuse_mount_namespaces_cache_by_account(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    import electridrive.vfs.fuse_mount as module
    roots = []
    class Binding:
        def __init__(self, tree, writable=False):
            roots.append(tree.cache_dir)
    monkeypatch.setattr(module, 'fuse_available', lambda: True)
    monkeypatch.setattr(module, 'create_operations_class', lambda: Binding)
    monkeypatch.setitem(sys.modules, 'fuse', SimpleNamespace(FUSE=lambda *a, **kw: None))
    for identity in ('account-one', 'account-two'):
        drive = FakeDrive()
        drive.account_key = identity
        mount = FuseMount(drive, tmp_path / 'cache')
        mount.start(str(tmp_path / 'mount'))
        mount.wait()
    assert len(roots) == 2 and roots[0] != roots[1]
    assert all(root.parent == tmp_path / 'cache' for root in roots)


def test_immediate_fuse_failure_is_reported_to_start_and_wait(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    import electridrive.vfs.fuse_mount as module
    def fail(*args, **kwargs):
        raise RuntimeError('mount permission denied')
    monkeypatch.setattr(module, 'fuse_available', lambda: True)
    monkeypatch.setattr(module, 'create_operations_class', lambda: lambda *a: object())
    monkeypatch.setitem(sys.modules, 'fuse', SimpleNamespace(FUSE=fail))
    mount = FuseMount(FakeDrive(), tmp_path / 'cache')
    with pytest.raises(RuntimeError, match='mount permission denied'):
        mount.start(str(tmp_path / 'mount'))
    with pytest.raises(RuntimeError, match='mount permission denied'):
        mount.wait()
    assert not mount.is_mounted


def test_fuse_binary_size_uses_remote_metadata_not_corrupt_cache(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    import electridrive.vfs.fuse_mount as module
    monkeypatch.setitem(sys.modules, 'fuse', SimpleNamespace(Operations=object, FuseOSError=OSError))
    drive = FakeDrive()
    drive.add_file('binary', b'complete')
    tree = DriveTree(drive, 'root', tmp_path)
    tree.cache_path(tree.resolve('/binary')).write_bytes(b'xx')
    operations = module.create_operations_class()(tree)
    assert operations.getattr('/binary')['st_size'] == len(b'complete')


def test_fuse_document_size_matches_verified_export(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    import electridrive.vfs.fuse_mount as module
    monkeypatch.setitem(sys.modules, 'fuse', SimpleNamespace(Operations=object, FuseOSError=OSError))
    drive = FakeDrive()
    drive.add_file('document', b'doc', mime='application/vnd.google-apps.document')
    tree = DriveTree(drive, 'root', tmp_path)
    operations = module.create_operations_class()(tree)
    assert operations.getattr('/document')['st_size'] == len(b'EXPORTED:doc')
    handle = operations.open('/document', 0)
    assert operations.read('/document', 100, 0, handle) == b'EXPORTED:doc'
    operations.release('/document', handle)


@pytest.mark.parametrize('corrupt', ['size', 'md5'])
def test_cache_rejects_incomplete_or_incorrect_payload_and_preserves_old(tmp_path, corrupt):
    drive = FakeDrive()
    fid = drive.add_file('a', b'old')
    tree = DriveTree(drive, 'root', tmp_path)
    old = tree.ensure_cached(tree.resolve('/a'))
    drive.nodes[fid]['content'] = b'updated'
    tree.invalidate()
    node = tree.resolve('/a')
    drive.download_file = lambda fid, dest: Path(dest).write_bytes(
        b'short' if corrupt == 'size' else b'wrong!!')
    with pytest.raises(ValueError):
        tree.ensure_cached(node)
    assert old.read_bytes() == b'old'
    assert not tree.cache_is_current(node)


def test_cache_root_identity_prevents_reuse_in_shared_directory(tmp_path):
    drive = FakeDrive()
    drive.add_file('a', b'data')
    first = DriveTree(drive, 'first-root', tmp_path)
    node = drive.get_metadata('file-1')
    first.ensure_cached(node)
    other = DriveTree(drive, 'other-root', tmp_path)
    assert not other.cache_is_current(node)


def test_unmount_missing_helper_or_live_thread_retains_ownership(tmp_path, monkeypatch):
    from types import SimpleNamespace
    mount = FuseMount(FakeDrive(), tmp_path)
    mount._thread = SimpleNamespace(is_alive=lambda: True, join=lambda timeout=None: None)
    mount.mountpoint = '/fake'
    monkeypatch.setattr('electridrive.vfs.fuse_mount.shutil.which', lambda _: None)
    with pytest.raises(RuntimeError, match='fusermount'):
        mount.stop()
    assert mount.mountpoint == '/fake'
    monkeypatch.setattr('electridrive.vfs.fuse_mount.shutil.which', lambda _: '/helper')
    monkeypatch.setattr('electridrive.vfs.fuse_mount.subprocess.run',
                        lambda *a, **k: SimpleNamespace(returncode=0))
    with pytest.raises(RuntimeError, match='exit'):
        mount.stop()
    assert mount.mountpoint == '/fake' and mount.is_mounted
