from argparse import Namespace
from types import SimpleNamespace

from electridrive.cli import cmd_unmount


def test_unmount_reports_helper_failure(monkeypatch, capsys):
    monkeypatch.setattr('shutil.which', lambda name: '/test/helper')
    monkeypatch.setattr('subprocess.run', lambda *a, **k: SimpleNamespace(returncode=1))
    assert cmd_unmount(Namespace(mountpoint='/test/mount')) == 1
    assert 'Unmounted' not in capsys.readouterr().out


def test_writable_mount_rejected_before_auth(monkeypatch):
    from electridrive.cli import cmd_mount
    import pytest
    monkeypatch.setattr('electridrive.cli.GoogleDriveClient', lambda: pytest.fail('auth attempted'))
    assert cmd_mount(Namespace(mountpoint='/test/mount', remote_folder='', writable=True)) == 2


def test_cli_download_does_not_overwrite_file_created_during_transfer(tmp_path, monkeypatch):
    from electridrive.cli import cmd_download
    from fakes import FakeDrive
    client = FakeDrive()
    fid = client.add_file('a', b'REMOTE')
    original = client.download_file
    def download(file_id, partial, progress_cb=None):
        (tmp_path / 'a').write_bytes(b'LOCAL')
        return original(file_id, partial, progress_cb)
    client.download_file = download
    monkeypatch.setattr('electridrive.cli.GoogleDriveClient', lambda: client)
    assert cmd_download(Namespace(file_id=fid, dest=str(tmp_path))) == 2
    assert (tmp_path / 'a').read_bytes() == b'LOCAL'


def test_sync_interval_runs_sequentially_and_stops_after_failure(tmp_path, monkeypatch):
    from electridrive import cli
    from electridrive.sync.twoway import SyncReport
    from fakes import FakeDrive
    drive = FakeDrive()
    monkeypatch.setattr(cli, 'GoogleDriveClient', lambda: drive)
    for key in ('XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_STATE_HOME'):
        monkeypatch.setenv(key, str(tmp_path / key))
    calls = []
    class Engine:
        def __init__(self, *args, **kwargs): pass
        def run(self):
            calls.append(1)
            return SyncReport(failed=1, errors=['offline'])
    monkeypatch.setattr('electridrive.sync.twoway.TwoWaySyncEngine', Engine)
    args = cli.build_parser().parse_args(['sync', str(tmp_path), '--remote-folder', 'remote', '--interval', '10'])
    assert cli.cmd_sync(args) == 2
    assert calls == [1]


def test_mount_immediate_failure_has_nonzero_exit(tmp_path, monkeypatch):
    from electridrive import cli
    monkeypatch.setattr(cli, 'configure_logging', lambda: None)
    monkeypatch.setattr('electridrive.vfs.fuse_available', lambda: True)
    monkeypatch.setattr(cli, 'GoogleDriveClient', lambda: object())
    def fail(*a, **kw):
        raise RuntimeError('binding refused mount')
    monkeypatch.setattr('electridrive.vfs.FuseMount.start', fail)
    assert cli.main(['mount', str(tmp_path / 'mount')]) != 0
