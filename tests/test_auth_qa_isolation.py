"""The credential lifecycle must retain the QA profile's safety boundary."""
import json
import os
import sys
from types import SimpleNamespace

import pytest

from electridrive import qa_policy as qa
from electridrive.config import DEFAULT_SCOPE, FULL_DRIVE_SCOPE, get_paths
from electridrive.google_api import oauth, app_client


@pytest.fixture
def profile(monkeypatch, tmp_path):
    monkeypatch.setattr(qa, '_active', None)
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'normal-config'))
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'normal-state'))
    monkeypatch.delenv('ELECTRIDRIVE_QA_PROFILE', raising=False)
    policy = qa.initialize(tmp_path / 'profile', tmp_path / 'data')
    monkeypatch.setenv('ELECTRIDRIVE_QA_PROFILE', str(policy.profile))
    def forbidden(*args, **kwargs):
        pytest.fail('ordinary credential source reached')
    monkeypatch.setitem(sys.modules, 'keyring', SimpleNamespace(
        get_password=forbidden, set_password=forbidden, delete_password=forbidden))
    monkeypatch.setattr(app_client, '_from_env', forbidden)
    monkeypatch.setattr(app_client, '_package_data', forbidden)
    monkeypatch.setattr(app_client, '_read_json', forbidden)
    return qa.current_policy()


def test_qa_lifecycle_checks_only_profile(profile):
    assert oauth.load_credentials() is None
    assert not oauth.has_saved_token()
    oauth.save_credentials(SimpleNamespace(to_json=lambda: json.dumps({'scopes': [DEFAULT_SCOPE]})))
    assert oauth.has_saved_token()
    oauth.clear_credentials()
    assert not oauth.has_saved_token()
    assert not app_client.is_configured()


@pytest.mark.parametrize('scopes', [None, [FULL_DRIVE_SCOPE], [DEFAULT_SCOPE, FULL_DRIVE_SCOPE]])
def test_qa_scope_rejection_preserves_isolated_token(profile, scopes):
    path = get_paths().token_fallback_file
    qa._write(path, {'scopes': scopes, 'token': 'isolated-token'})
    before = path.read_bytes()
    with pytest.raises(oauth.OAuthError, match='QA'):
        oauth.load_credentials()
    assert path.read_bytes() == before


@pytest.mark.parametrize('kind', ['symlink', 'dangling', 'hardlink'])
@pytest.mark.parametrize('name', ['token', 'credentials', 'app_client'])
def test_qa_credential_files_reject_links(profile, tmp_path, kind, name):
    paths = get_paths()
    target = tmp_path / 'outside'
    target.write_text(json.dumps({'installed': {'client_id': 'outside'}}))
    target.chmod(0o600)
    path = {'token': paths.token_fallback_file, 'credentials': paths.credentials_file,
            'app_client': paths.config_dir / 'app_client.json'}[name]
    if kind == 'hardlink':
        os.link(target, path)
    else:
        path.symlink_to(target if kind == 'symlink' else tmp_path / 'missing')
    if name == 'token':
        for operation in (oauth.load_credentials, oauth.has_saved_token, oauth.clear_credentials):
            with pytest.raises(qa.QaSafetyError):
                operation()
        with pytest.raises(qa.QaSafetyError):
            oauth.save_credentials(SimpleNamespace(to_json=lambda: '{}'))
    else:
        with pytest.raises(qa.QaSafetyError):
            app_client.client_config()
    assert target.read_text() == json.dumps({'installed': {'client_id': 'outside'}})


def test_qa_token_path_mismatch_rejected(profile, tmp_path):
    for operation in (lambda: oauth._load_token_from_file(tmp_path / 'other'),
                      lambda: oauth._save_token_to_file(tmp_path / 'other', '{}')):
        with pytest.raises(oauth.OAuthError, match='path mismatch'):
            operation()


def test_qa_own_client_shared_resolver(profile):
    qa._write(get_paths().credentials_file, {'installed': {'client_id': 'qa-own'}})
    qa._write(get_paths().config_dir / 'app_client.json', {'installed': {'client_id': 'qa-other'}})
    assert app_client.client_id() == 'qa-own'
    assert app_client.client_config()['installed']['client_id'] == 'qa-own'


@pytest.mark.parametrize('operation', ['load_credentials', 'has_saved_token'])
def test_qa_null_token_fails_closed_without_normal_sources(profile, operation):
    path = get_paths().token_fallback_file
    qa._write(path, None)
    before = path.read_bytes()
    with pytest.raises(oauth.OAuthError, match='token'):
        getattr(oauth, operation)()
    assert path.read_bytes() == before
