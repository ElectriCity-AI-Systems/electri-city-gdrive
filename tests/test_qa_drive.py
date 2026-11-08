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


@pytest.fixture
def registered_root(policy):
    from dataclasses import replace

    class SyntheticDrive(FakeDrive):
        def _to_remote(self, node):
            return replace(
                super()._to_remote(node),
                app_properties=node.get('app_properties', {}),
            )

    fake = SyntheticDrive()
    root_id = fake.add_folder('synthetic')
    fake.nodes[root_id]['app_properties'] = {
        'electridrive_qa_nonce': policy.nonce,
    }
    policy.register_new_root(fake.get_metadata(root_id), 'synthetic')
    return fake, QaDriveClient(fake, policy), root_id


def test_unknown_id_refused_before_remote_read(policy):
    class Spy(FakeDrive):
        def get_metadata(self, rid):
            pytest.fail('Unknown ID must not be fetched')
    client = QaDriveClient(Spy(), policy)
    with pytest.raises(qa.QaSafetyError) as caught:
        client.get_metadata('personal')
    assert caught.value.rule == 'cloud-id-not-registered'
    assert client.list_folder().files == []


def test_wide_operations_are_closed(policy):
    client = QaDriveClient(FakeDrive(), policy)
    expected_rules = {
        'search': 'cloud-search-disabled',
        'list_files': 'cloud-list-all-disabled',
        'list_starred': 'cloud-starred-list-disabled',
        'list_trash': 'cloud-trash-list-disabled',
        'trash': 'cloud-trash-disabled',
        'untrash': 'cloud-untrash-disabled',
    }
    for name, rule in expected_rules.items():
        with pytest.raises(qa.QaSafetyError) as caught:
            getattr(client, name)('anything')
        assert caught.value.rule == rule
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


def test_customer_candidate_qa_uses_validated_embedded_publisher_client(
    policy, monkeypatch, tmp_path,
):
    """QA must isolate tokens/Drive roots, not reject a valid public package client."""
    import json
    from electridrive.google_api import app_client, oauth

    baked = tmp_path / "synthetic-publisher.json"
    data = {
        "installed": {
            "client_id": "1234567890-abcdefghijklmnop.apps.googleusercontent.com",
            "client_secret": "synthetic-public-desktop-secret",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "redirect_uris": ["http://localhost"],
        },
        "picker_api_key": "AIza" + "A" * 35,
    }
    baked.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(app_client, "build_channel", lambda: "CUSTOMER-CANDIDATE")
    monkeypatch.setattr(app_client, "_PACKAGE_FILE", baked)
    monkeypatch.setenv("ELECTRIDRIVE_CLIENT_ID", "untrusted-ordinary-client")
    monkeypatch.setenv("ELECTRIDRIVE_PICKER_API_KEY", "untrusted-ordinary-picker")
    monkeypatch.setattr(
        app_client, "_user_file_data",
        lambda: pytest.fail("QA customer client must not consult profile or ordinary clients"),
    )
    monkeypatch.setattr(
        app_client, "_from_env",
        lambda: pytest.fail("QA customer client must not consult environment credentials"),
    )

    assert app_client.client_config() == {"installed": data["installed"]}
    assert app_client.client_id() == data["installed"]["client_id"]
    assert app_client.picker_api_key() == data["picker_api_key"]
    assert app_client.is_configured()
    assert app_client.is_picker_configured()
    assert oauth._try_keyring_get() is None
    policy.verify()
    assert policy.remote_ids == ()


def test_customer_candidate_qa_rejects_missing_or_invalid_embedded_client(
    policy, monkeypatch, tmp_path,
):
    import json
    from electridrive.google_api import app_client

    baked = tmp_path / "synthetic-invalid-client.json"
    monkeypatch.setattr(app_client, "build_channel", lambda: "CUSTOMER-CANDIDATE")
    monkeypatch.setattr(app_client, "_PACKAGE_FILE", baked)
    monkeypatch.setattr(
        app_client, "_user_file_data",
        lambda: pytest.fail("Missing customer package must not fall back to profile"),
    )
    with pytest.raises(ValueError, match="unavailable"):
        app_client.client_config()
    with pytest.raises(ValueError, match="unavailable"):
        app_client.picker_api_key()

    baked.write_text(json.dumps({"installed": {"client_id": "REPLACE_WITH_YOUR_CLIENT_ID"},
                                 "picker_api_key": "REPLACE_WITH_YOUR_PICKER_API_KEY"}),
                     encoding="utf-8")
    with pytest.raises(ValueError, match="placeholder"):
        app_client.client_config()
    with pytest.raises(ValueError, match="placeholder"):
        app_client.picker_api_key()
    policy.verify()


def test_ci_qa_still_refuses_embedded_customer_credentials(policy, monkeypatch, tmp_path):
    from electridrive.google_api import app_client

    monkeypatch.setattr(app_client, "build_channel", lambda: "CI-QA")
    monkeypatch.setattr(app_client, "_PACKAGE_FILE", tmp_path / "no-publisher-client.json")
    monkeypatch.setenv("ELECTRIDRIVE_CLIENT_ID", "ordinary-client")
    monkeypatch.setenv("ELECTRIDRIVE_PICKER_API_KEY", "ordinary-picker")
    assert not app_client.is_configured()
    assert not app_client.is_picker_configured()
    policy.verify()


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
    monkeypatch.setattr(module, 'authenticate_interactive', object)
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


def test_same_name_uploads_register_and_revalidate_each_qa_file_id(
    registered_root, policy,
):
    fake, client, root_id = registered_root
    sources = []
    for name, content in (
        ('duplicate-a.bin', b'artificial duplicate A'),
        ('duplicate-b.bin', b'artificial duplicate B'),
    ):
        source = policy.local_root / name
        source.write_bytes(content)
        sources.append((source, content))

    first_id = client.upload_file(sources[0][0], root_id, 'same-name.txt')
    second_id = client.upload_file(sources[1][0], root_id, 'same-name.txt')

    assert first_id != second_id
    assert client.get_metadata(first_id).parents == (root_id,)
    assert client.get_metadata(second_id).parents == (root_id,)
    matches = [
        item for item in client.list_folder(root_id).files
        if item.name == 'same-name.txt'
    ]
    assert {item.id for item in matches} == {first_id, second_id}
    assert len(fake.uploads) == 2 and not fake.updates
    assert fake.nodes[first_id]['content'] == sources[0][1]
    assert fake.nodes[second_id]['content'] == sources[1][1]


def test_qa_metadata_revalidation_logs_exact_parent_guard_if_file_leaves_root(
    registered_root, policy, caplog,
):
    fake, client, root_id = registered_root
    source = policy.local_root / 'duplicate.bin'
    source.write_bytes(b'artificial duplicate')
    file_id = client.upload_file(source, root_id, 'same-name.txt')
    assert client.get_metadata(file_id).parents == (root_id,)

    outside = fake.add_folder('outside')
    fake.nodes[file_id]['parent'] = outside

    with caplog.at_level('WARNING', logger='electridrive.qa.safety'):
        with pytest.raises(qa.QaSafetyError) as caught:
            client.get_metadata(file_id)
    record = next(record for record in caplog.records
                  if record.name == 'electridrive.qa.safety')
    assert caught.value.category == 'cloud'
    assert caught.value.rule == 'cloud-parent-mismatch'
    assert record.qa_safety_category == 'cloud'
    assert record.qa_safety_rule == 'cloud-parent-mismatch'
    assert record.qa_safety_reason == 'A cloud object is not in its registered parent folder.'
    assert 'outside' not in record.getMessage()
    assert 'artificial duplicate' not in record.getMessage()
    assert not fake.updates and not fake.nodes[file_id]['trashed']


def test_qa_safety_diagnostic_does_not_log_untrusted_path(policy, tmp_path, caplog):
    secret_marker = 'private-fixture-path-marker'
    outside = tmp_path / secret_marker
    with caplog.at_level('WARNING', logger='electridrive.qa.safety'):
        with pytest.raises(qa.QaSafetyError) as caught:
            policy.local(outside)

    record = next(record for record in caplog.records
                  if record.name == 'electridrive.qa.safety')
    assert caught.value.rule == 'local-path-boundary'
    assert record.qa_safety_category == 'filesystem'
    assert record.qa_safety_rule == 'local-path-boundary'
    assert secret_marker not in record.getMessage()
    assert secret_marker not in str(caught.value)


def test_unknown_qa_safety_reason_is_replaced_with_safe_diagnostic(caplog):
    secret_marker = 'token-like-private-value'
    with caplog.at_level('WARNING', logger='electridrive.qa.safety'):
        error = qa.QaSafetyError(f'unknown guard: {secret_marker}')

    record = next(record for record in caplog.records
                  if record.name == 'electridrive.qa.safety')
    assert error.rule == 'unclassified-qa-safety-guard'
    assert record.qa_safety_category == 'internal'
    assert secret_marker not in record.getMessage()
    assert secret_marker not in str(error)


def test_safety_stop_does_not_get_relogged_with_context(caplog):
    import logging

    secret_marker = 'private-file-context'
    logger = logging.getLogger('fixture.context')
    with caplog.at_level('WARNING'):
        try:
            raise qa.QaSafetyError('local path is outside the synthetic QA data directory')
        except qa.QaSafetyError:
            qa.log_exception(logger, 'sync failed for %s', secret_marker)

    assert not any(record.name == 'fixture.context' for record in caplog.records)
    diagnostic = next(record for record in caplog.records
                      if record.name == 'electridrive.qa.safety')
    assert diagnostic.qa_safety_rule == 'local-path-boundary'
    assert secret_marker not in diagnostic.getMessage()


def test_unrelated_exceptions_keep_existing_error_logging(caplog):
    import logging

    logger = logging.getLogger('fixture.context')
    with caplog.at_level('ERROR'):
        try:
            raise ValueError('ordinary failure')
        except ValueError:
            qa.log_exception(logger, 'operation failed: %s', 'fixture')

    record = next(record for record in caplog.records
                  if record.name == 'fixture.context')
    assert record.getMessage() == 'operation failed: fixture'
    assert record.exc_info[0] is ValueError
