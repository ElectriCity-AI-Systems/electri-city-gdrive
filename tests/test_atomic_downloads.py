from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys

import pytest

from electridrive.google_api.client import GoogleDriveClient
from electridrive.transfers.manager import TransferCanceled


@pytest.fixture(params=["download_file", "export_file"])
def transfer(request, monkeypatch):
    options = {"fail": False}

    class Downloader:
        def __init__(self, handle, _request):
            self.handle = handle
            self.chunks = 0

        def next_chunk(self):
            self.chunks += 1
            if self.chunks == 2 and options["fail"]:
                raise OSError("network interrupted")
            self.handle.write(b"part" if self.chunks == 1 else b"-complete")
            done = self.chunks == 2
            status = SimpleNamespace(
                resumable_progress=13 if done else 4, total_size=13,
            )
            return status, done

    http = ModuleType("googleapiclient.http")
    http.MediaIoBaseDownload = Downloader
    monkeypatch.setitem(sys.modules, "googleapiclient.http", http)
    files = SimpleNamespace(
        get_media=lambda **_kwargs: object(),
        export_media=lambda **_kwargs: object(),
    )
    client = GoogleDriveClient(service=SimpleNamespace(files=lambda: files))

    def run(destination, progress=None):
        if request.param == "export_file":
            return client.export_file("remote-id", destination, "text/plain", progress)
        return client.download_file("remote-id", destination, progress)

    return run, options


def test_complete_transfer_replaces_only_after_last_callback(transfer, tmp_path):
    run, _options = transfer
    target = tmp_path / "report.txt"
    original = b"previous complete local file"
    target.write_bytes(original)
    target.chmod(0o640)
    progress_calls = []

    def progress(done, total):
        assert target.read_bytes() == original
        progress_calls.append((done, total))

    assert run(target, progress) == target
    assert target.read_bytes() == b"part-complete"
    assert target.stat().st_mode & 0o777 == 0o640
    assert progress_calls == [(4, 13), (13, 13)]
    assert set(tmp_path.iterdir()) == {target}


@pytest.mark.parametrize("existing", [False, True])
def test_network_interruption_preserves_destination_and_cleans_partial(
    transfer, tmp_path, existing,
):
    run, options = transfer
    options["fail"] = True
    target = tmp_path / "report.txt"
    original = b"previous complete local file"
    if existing:
        target.write_bytes(original)

    with pytest.raises(OSError, match="network interrupted"):
        run(target)

    if existing:
        assert target.read_bytes() == original
    else:
        assert not target.exists()
    assert set(tmp_path.iterdir()) == ({target} if existing else set())


def test_cancel_from_progress_preserves_existing_destination(transfer, tmp_path):
    run, _options = transfer
    target = tmp_path / "report.txt"
    original = b"previous complete local file"
    target.write_bytes(original)

    def cancel(_done, _total):
        raise TransferCanceled()

    with pytest.raises(TransferCanceled):
        run(target, cancel)

    assert target.read_bytes() == original
    assert set(tmp_path.iterdir()) == {target}


def test_replace_failure_preserves_existing_destination(transfer, tmp_path, monkeypatch):
    run, _options = transfer
    target = tmp_path / "report.txt"
    original = b"previous complete local file"
    target.write_bytes(original)

    def fail_replace(_source, _destination):
        raise PermissionError("replacement denied")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(PermissionError, match="replacement denied"):
        run(target)

    assert target.read_bytes() == original
    assert set(tmp_path.iterdir()) == {target}


def test_new_destination_can_have_a_long_name(transfer, tmp_path):
    run, _options = transfer
    target = tmp_path / ("x" * 250)

    assert run(target) == target
    assert target.read_bytes() == b"part-complete"
    assert set(tmp_path.iterdir()) == {target}
