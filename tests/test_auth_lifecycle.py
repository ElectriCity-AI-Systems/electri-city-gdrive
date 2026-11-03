import json
from types import SimpleNamespace

import pytest
from google.auth.exceptions import RefreshError, TransportError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from electridrive.config import DEFAULT_SCOPE, FULL_DRIVE_SCOPE, get_paths
from electridrive.google_api import oauth

REAL_KEYRING_GET = oauth._try_keyring_get


@pytest.fixture(autouse=True)
def isolated_auth(monkeypatch, tmp_path):
    import keyring
    monkeypatch.setattr(keyring, 'delete_password', lambda *args: None)
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    monkeypatch.delenv('ELECTRIDRIVE_SCOPE', raising=False)
    monkeypatch.setattr(oauth, '_try_keyring_get', lambda: None)
    monkeypatch.setattr(oauth, '_try_keyring_set', lambda token: False)


def test_scope_change_does_not_relabel_token():
    path = get_paths().token_fallback_file
    path.write_text(json.dumps({'token': 'full-token', 'refresh_token': 'refresh',
        'client_id': 'client', 'client_secret': 'secret', 'scopes': [FULL_DRIVE_SCOPE]}))
    assert oauth.load_credentials() is None
    assert not path.exists()


def test_saved_scopes_are_preserved():
    original = Credentials('token', refresh_token='refresh', token_uri='https://oauth2.googleapis.com/token',
                           client_id='client', client_secret='secret', scopes=[DEFAULT_SCOPE])
    oauth.save_credentials(original)
    assert oauth.load_credentials().scopes == [DEFAULT_SCOPE]


def test_successful_keyring_save_removes_stale_file(monkeypatch):
    path = get_paths().token_fallback_file
    path.write_text('old account')
    monkeypatch.setattr(oauth, '_try_keyring_set', lambda token: True)
    oauth.save_credentials(SimpleNamespace(to_json=lambda: '{}'))
    assert not path.exists()


@pytest.mark.parametrize('error', [RefreshError('invalid_grant: revoked', {'error': 'invalid_grant'}),
                                  TransportError('offline'),
                                  RefreshError('temporarily_unavailable', {'error': 'temporarily_unavailable'})])
def test_only_invalid_grant_reauthorizes(monkeypatch, error):
    path = get_paths().token_fallback_file
    path.write_text('saved token')
    def refresh(request):
        raise error
    old = SimpleNamespace(valid=False, expired=True, refresh_token='refresh', refresh=refresh)
    monkeypatch.setattr(oauth, 'load_credentials', lambda: old)
    new = SimpleNamespace(to_json=lambda: '{}', scopes=[DEFAULT_SCOPE])
    monkeypatch.setattr(InstalledAppFlow, 'from_client_config',
                        lambda *a, **k: SimpleNamespace(run_local_server=lambda **kw: new))
    monkeypatch.setenv('ELECTRIDRIVE_CLIENT_ID', 'example.apps.googleusercontent.com')
    if isinstance(error, RefreshError) and error.args[1]['error'] == 'invalid_grant':
        assert oauth.authenticate_interactive() is new
        assert path.read_text() == '{}'
    else:
        with pytest.raises(type(error)):
            oauth.authenticate_interactive()
        assert path.read_text() == 'saved token'


def test_keyring_error_is_distinct_from_absent_token(monkeypatch):
    import keyring
    monkeypatch.setattr(oauth, '_try_keyring_get', REAL_KEYRING_GET)
    monkeypatch.setattr(keyring, 'get_password', lambda *a: (_ for _ in ()).throw(RuntimeError('locked')))
    with pytest.raises(oauth.OAuthError, match='keyring'):
        oauth._try_keyring_get()


def test_save_records_actual_granted_scopes():
    creds = Credentials('token', refresh_token='refresh', token_uri='https://oauth2.googleapis.com/token',
                        client_id='client', client_secret='secret', scopes=[DEFAULT_SCOPE],
                        granted_scopes=[FULL_DRIVE_SCOPE])
    oauth.save_credentials(creds)
    assert oauth.load_credentials() is None


def test_absent_keyring_backend_allows_new_login(monkeypatch):
    import keyring
    from keyring.errors import NoKeyringError
    monkeypatch.setattr(oauth, '_try_keyring_get', REAL_KEYRING_GET)
    monkeypatch.setattr(keyring, 'get_password', lambda *a: (_ for _ in ()).throw(NoKeyringError('no backend')))
    assert oauth._try_keyring_get() is None


def test_new_login_rejects_broader_actual_grant(monkeypatch):
    monkeypatch.setattr(oauth, 'load_credentials', lambda: None)
    new = Credentials('full', scopes=[DEFAULT_SCOPE], granted_scopes=[FULL_DRIVE_SCOPE])
    monkeypatch.setattr(InstalledAppFlow, 'from_client_config',
                        lambda *a, **k: SimpleNamespace(run_local_server=lambda **kw: new))
    monkeypatch.setenv('ELECTRIDRIVE_CLIENT_ID', 'example.apps.googleusercontent.com')
    with pytest.raises(oauth.OAuthError, match='scope'):
        oauth.authenticate_interactive()
    assert not get_paths().token_fallback_file.exists()


def test_fallback_wins_over_stale_keyring(monkeypatch):
    path = get_paths().token_fallback_file
    path.write_text(json.dumps({'token': 'new', 'refresh_token': 'refresh',
        'client_id': 'client', 'client_secret': 'secret', 'scopes': [DEFAULT_SCOPE]}))
    monkeypatch.setattr(oauth, '_try_keyring_get', lambda: pytest.fail('stale keyring consulted'))
    assert oauth.load_credentials().token == 'new'


def test_malformed_fallback_fails_closed(monkeypatch):
    get_paths().token_fallback_file.write_text('not json')
    monkeypatch.setattr(oauth, '_try_keyring_get', lambda: pytest.fail('stale keyring consulted'))
    with pytest.raises(oauth.OAuthError, match='token'):
        oauth.load_credentials()


def test_validate_scopes_normalizes_string_metadata():
    oauth.validate_scopes(SimpleNamespace(scopes=DEFAULT_SCOPE), [DEFAULT_SCOPE])


def test_own_client_wins_in_shared_resolver(monkeypatch):
    from electridrive.google_api import app_client
    get_paths().credentials_file.write_text(json.dumps({'installed': {'client_id': 'own-client'}}))
    monkeypatch.setenv('ELECTRIDRIVE_CLIENT_ID', 'env-client')
    assert app_client.client_config()['installed']['client_id'] == 'own-client'
    assert app_client.client_id() == 'own-client'


def test_valid_loaded_token_checks_actual_scope(monkeypatch):
    monkeypatch.setattr(oauth, 'load_credentials', lambda: SimpleNamespace(
        valid=True, scopes=[DEFAULT_SCOPE], granted_scopes=[FULL_DRIVE_SCOPE]))
    with pytest.raises(oauth.OAuthError, match='scope'):
        oauth.authenticate_interactive()


def test_has_saved_token_checks_keyring(monkeypatch):
    monkeypatch.setattr(oauth, '_try_keyring_get', lambda: 'cached')
    assert oauth.has_saved_token()


def test_refreshed_token_checks_actual_grants_before_save(monkeypatch):
    path = get_paths().token_fallback_file
    path.write_text('saved refresh token')
    old = SimpleNamespace(valid=False, expired=True, refresh_token='refresh',
                          scopes=[DEFAULT_SCOPE], granted_scopes=[FULL_DRIVE_SCOPE],
                          refresh=lambda request: None)
    monkeypatch.setattr(oauth, 'load_credentials', lambda: old)
    with pytest.raises(oauth.OAuthError, match='scope'):
        oauth.authenticate_interactive()
    assert path.read_text() == 'saved refresh token'


@pytest.mark.parametrize('operation', ['load_credentials', 'has_saved_token'])
@pytest.mark.parametrize('malformed', ['null', '[]', '"invalid token"', '42'])
def test_existing_nonobject_fallback_never_restores_stale_keyring(monkeypatch, operation, malformed):
    path = get_paths().token_fallback_file
    path.write_text(malformed)
    keyring_reads = []
    def stale_token():
        keyring_reads.append(True)
        return json.dumps({'token': 'stale-account', 'refresh_token': 'refresh',
            'client_id': 'client', 'client_secret': 'secret', 'scopes': [DEFAULT_SCOPE]})
    monkeypatch.setattr(oauth, '_try_keyring_get', stale_token)
    with pytest.raises(oauth.OAuthError, match='token'):
        getattr(oauth, operation)()
    assert keyring_reads == []
    assert path.read_text() == malformed
