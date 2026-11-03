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


def test_sync_up_cli_refuses_untracked_file_without_confirmation(tmp_path, monkeypatch):
    from electridrive import cli
    from fakes import FakeDrive
    for key in ('XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_STATE_HOME'):
        monkeypatch.setenv(key, str(tmp_path / key))
    local = tmp_path / 'local'
    local.mkdir()
    (local / 'shared.txt').write_bytes(b'local-content')
    drive = FakeDrive()
    remote_root = drive.ensure_folder_path('ElectriDrive/CliConfirm')
    wild_id = drive.add_file('shared.txt', b'pre-existing, never tracked', parent=remote_root)
    monkeypatch.setattr(cli, 'GoogleDriveClient', lambda: drive)
    args = cli.build_parser().parse_args(
        ['sync-up', str(local), '--remote-folder', 'ElectriDrive/CliConfirm'])
    assert cli.cmd_sync_up(args) == 2
    assert drive.nodes[wild_id]['content'] == b'pre-existing, never tracked'


def test_sync_up_cli_confirm_remote_id_flag_allows_adoption(tmp_path, monkeypatch):
    from electridrive import cli
    from fakes import FakeDrive
    for key in ('XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_STATE_HOME'):
        monkeypatch.setenv(key, str(tmp_path / key))
    local = tmp_path / 'local'
    local.mkdir()
    (local / 'shared.txt').write_bytes(b'local-content')
    drive = FakeDrive()
    remote_root = drive.ensure_folder_path('ElectriDrive/CliConfirm')
    wild_id = drive.add_file('shared.txt', b'pre-existing, never tracked', parent=remote_root)
    monkeypatch.setattr(cli, 'GoogleDriveClient', lambda: drive)
    args = cli.build_parser().parse_args([
        'sync-up', str(local), '--remote-folder', 'ElectriDrive/CliConfirm',
        '--confirm-remote-id', f'shared.txt={wild_id}',
    ])
    assert cli.cmd_sync_up(args) == 0
    assert drive.nodes[wild_id]['content'] == b'local-content'


def test_parse_confirmed_remote_ids_rejects_malformed_entries():
    import pytest
    from electridrive.cli import _parse_confirmed_remote_ids
    assert _parse_confirmed_remote_ids(None) == {}
    assert _parse_confirmed_remote_ids(['a.txt=1abc']) == {'a.txt': '1abc'}
    for bad in ('no-equals-sign', '=missing-rel', 'missing-id='):
        with pytest.raises(ValueError):
            _parse_confirmed_remote_ids([bad])


def test_mount_immediate_failure_has_nonzero_exit(tmp_path, monkeypatch):
    from electridrive import cli
    monkeypatch.setattr(cli, 'configure_logging', lambda: None)
    monkeypatch.setattr('electridrive.vfs.fuse_available', lambda: True)
    monkeypatch.setattr(cli, 'GoogleDriveClient', object)
    def fail(*a, **kw):
        raise RuntimeError('binding refused mount')
    monkeypatch.setattr('electridrive.vfs.FuseMount.start', fail)
    assert cli.main(['mount', str(tmp_path / 'mount')]) != 0
