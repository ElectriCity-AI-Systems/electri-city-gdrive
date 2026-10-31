import pytest
import sys
import types
from unittest.mock import MagicMock
from electridrive.google_api.client import GoogleDriveClient

def test_account_key_uses_stable_permission_identity():
    service = MagicMock()
    service.about().get().execute.return_value = {'user': {'permissionId': 'stable', 'emailAddress': 'a@b'}}
    client = GoogleDriveClient(service=service)
    assert client.account_key == 'permission:stable'


def test_upload_preserves_name_based_mime_for_pinned_source(tmp_path, monkeypatch):
    observed = []
    def media(path, **kwargs):
        observed.append(kwargs)
        return object()
    monkeypatch.setitem(sys.modules, 'googleapiclient.http', types.SimpleNamespace(MediaFileUpload=media))
    service = MagicMock()
    service.files().create().next_chunk.return_value = (None, {'id': 'image'})
    source = tmp_path / '123'
    source.write_bytes(b'image data')
    GoogleDriveClient(service=service).upload_file(source, 'parent', 'photo.jpg')
    assert observed[0]['mimetype'] == 'image/jpeg'


def test_update_preserves_remote_mime_for_pinned_source(tmp_path, monkeypatch):
    observed = []
    def media(path, **kwargs):
        observed.append(kwargs)
        return object()
    monkeypatch.setitem(sys.modules, 'googleapiclient.http', types.SimpleNamespace(MediaFileUpload=media))
    service = MagicMock()
    service.files().get().execute.return_value = {'id': 'image', 'name': 'photo.png', 'mimeType': 'image/png'}
    service.files().update().next_chunk.return_value = (None, {'id': 'image'})
    source = tmp_path / '123'
    source.write_bytes(b'image data')
    GoogleDriveClient(service=service).update_file('image', source)
    assert observed[0]['mimetype'] == 'image/png'


def test_download_rejects_completed_but_incorrect_bytes(tmp_path, monkeypatch):
    class ShortDownload:
        def __init__(self, handle, request):
            self.handle = handle
        def next_chunk(self, **kwargs):
            self.handle.write(b'BAD')
            return None, True
    monkeypatch.setitem(sys.modules, 'googleapiclient.http', types.SimpleNamespace(MediaIoBaseDownload=ShortDownload))
    service = MagicMock()
    service.files().get().execute.return_value = {
        'id': 'id', 'name': 'a', 'size': '4', 'md5Checksum': '45802158e78dd9584161629098018fe8'}
    client = GoogleDriveClient(service=service)
    dest = tmp_path / 'a'
    dest.write_bytes(b'ORIGINAL')
    with pytest.raises(ValueError, match='[Ii]ntegrity'):
        client.download_file('id', dest)
    assert dest.read_bytes() == b'ORIGINAL'


