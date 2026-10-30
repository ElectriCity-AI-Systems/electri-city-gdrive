"""Offline regressions for closed QA cloud capabilities."""
import pytest
from electridrive import qa_policy as qa
from electridrive.google_api.client import GoogleDriveClient, RemoteFile
from electridrive.qa_drive import QaDriveClient
from fakes import FakeDrive


@pytest.fixture
def policy(monkeypatch, tmp_path):
    monkeypatch.setattr(qa, '_active', None)
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path/'normal-config'))
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path/'normal-state'))
    monkeypatch.delenv('ELECTRIDRIVE_QA_PROFILE', raising=False)
    p = qa.initialize(tmp_path/'profile', tmp_path/'data')
    monkeypatch.setenv('ELECTRIDRIVE_QA_PROFILE', str(p.profile))
    return qa.current_policy()


def test_unknown_id_refused_before_remote_read(policy):
    class Spy(FakeDrive):
        def get_metadata(self, rid):
            pytest.fail('Unknown ID must not be fetched')
    client = QaDriveClient(Spy(), policy)
    with pytest.raises(qa.QaSafetyError):
        client.get_metadata('personal')
    assert client.list_folder().files == []


def test_wide_operations_are_closed(policy):
    client = QaDriveClient(FakeDrive(), policy)
    for name in ('search', 'list_files', 'list_starred', 'list_trash', 'trash', 'untrash'):
        with pytest.raises(qa.QaSafetyError):
            getattr(client, name)('anything')
    assert client.get_start_page_token() is None
    assert client.list_changes('anything') is None


def test_google_injection_rejected(policy):
    for kwargs in ({'service': object()}, {'credentials': object()}):
        with pytest.raises(qa.QaSafetyError):
            GoogleDriveClient(**kwargs)


def test_old_normal_client_rejected(policy):
    raw = object.__new__(GoogleDriveClient)
    raw._qa_profile = None
    with pytest.raises(qa.QaSafetyError):
        QaDriveClient(raw, policy)
    with pytest.raises(qa.QaSafetyError):
        raw.get_metadata('personal')


def test_new_root_creation_is_explicit_and_nonce_bound(policy):
    class Synthetic(FakeDrive):
        def _create_qa_folder(self, name, nonce):
            from dataclasses import replace
            rid = self.add_folder(name)
            self.metadata = replace(super().get_metadata(rid), app_properties={'electridrive_qa_nonce': nonce})
            return self.metadata
        def get_metadata(self, rid):
            return self.metadata if rid == self.metadata.id else super().get_metadata(rid)
    fake = Synthetic()
    client = QaDriveClient(fake, policy)
    root = client.create_qa_root('synthetic')
    assert client.ensure_folder_path(root) == root[3:]
    assert len(client.list_folder().files) == 1


def test_atomic_download_denies_outside_destination(policy, tmp_path):
    from electridrive.google_api.client import _atomic_download_file
    with pytest.raises(qa.QaSafetyError):
        with _atomic_download_file(tmp_path/'personal'):
            pytest.fail('Outside writer opened')
    target = policy.local_root/'ok'
    with _atomic_download_file(target) as stream:
        stream.write(b'artificial')
    assert target.read_bytes() == b'artificial'


def test_qa_oauth_never_uses_keyring_or_environment(policy, monkeypatch):
    import sys
    from types import SimpleNamespace
    from electridrive.google_api import oauth, app_client
    def forbidden(*args, **kwargs):
        pytest.fail('Ordinary credential source reached')
    monkeypatch.setitem(sys.modules, 'keyring', SimpleNamespace(get_password=forbidden, set_password=forbidden, delete_password=forbidden))
    monkeypatch.setenv('ELECTRIDRIVE_CLIENT_ID', 'ordinary-client')
    monkeypatch.setenv('ELECTRIDRIVE_PICKER_API_KEY', 'ordinary-picker')
    assert oauth._try_keyring_get() is None
    assert oauth._try_keyring_set('{}') is False
    assert app_client.client_id() != 'ordinary-client'
    assert app_client.picker_api_key() != 'ordinary-picker'
    oauth.clear_credentials()


def test_qa_oauth_token_symlink_is_refused(policy, tmp_path):
    from electridrive.google_api import oauth
    from electridrive.config import get_paths
    personal = tmp_path/'personal-token'
    personal.write_text('private fixture')
    get_paths().token_fallback_file.symlink_to(personal)
    with pytest.raises(qa.QaSafetyError):
        oauth._load_token_from_file(get_paths().token_fallback_file)
    with pytest.raises(qa.QaSafetyError):
        oauth._save_token_to_file(get_paths().token_fallback_file, '{}')
    assert personal.read_text() == 'private fixture'


def test_qa_google_construction_autowraps(policy, monkeypatch):
    import electridrive.google_api.client as module
    monkeypatch.setattr(module, 'authenticate_interactive', lambda: object())
    assert isinstance(module.GoogleDriveClient(), QaDriveClient)


def test_quota_identity_is_available_for_connection(policy):
    client = QaDriveClient(FakeDrive(), policy)
    assert client.get_about().user_email == 'tester@example.com'


@pytest.mark.parametrize('name', ['../escape', 'nested/name', 'nested\\name', '.', '..', ''])
def test_remote_names_are_single_components(policy, name):
    client = QaDriveClient(FakeDrive(), policy)
    with pytest.raises(qa.QaSafetyError):
        client.create_qa_root(name)


def test_native_workspace_media_update_is_refused(policy):
    from dataclasses import replace
    class Synthetic(FakeDrive):
        def get_metadata(self, rid):
            remote = super().get_metadata(rid)
            return replace(remote, app_properties={'electridrive_qa_nonce': policy.nonce})
    fake = Synthetic()
    rid = fake.add_folder('synthetic')
    policy.register_new_root(fake.get_metadata(rid), 'synthetic')
    fid = fake.add_file('document', parent=rid, mime='application/vnd.google-apps.document')
    client = QaDriveClient(fake, policy)
    client.list_folder(rid)
    source = policy.local_root/'artificial'
    source.write_bytes(b'artificial')
    with pytest.raises(qa.QaSafetyError):
        client.update_file(fid, source)
    assert not fake.updates
