from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from electridrive.google_api.release_config import (
    customer_config_errors,
    validate_customer_config,
)
from scripts.verify_customer_artifact import verify

_REVISION = "a" * 40


def _client() -> dict:
    return {
        "installed": {
            "client_id": "1234567890-abcdefghijklmnopqrstuv.apps.googleusercontent.com",
            "client_secret": "publisher-secret",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "redirect_uris": ["http://localhost"],
        },
        "picker_api_key": "AIza" + "A" * 35,
    }


def _write_build(root: Path, channel: str, config: dict | None = None) -> Path:
    app_root = root / "_internal" / "electridrive"
    app_root.mkdir(parents=True, exist_ok=True)
    (app_root / "build_channel.txt").write_text(channel + "\n", encoding="utf-8")
    (app_root / "build_provenance.json").write_text(
        json.dumps(
            {
                "repository": "ElectriCity-AI-Systems/electri-city-gdrive",
                "source_revision": _REVISION,
                "source_tree_clean": True,
                "channel": channel,
            }
        ),
        encoding="utf-8",
    )
    if config is not None:
        google_api = app_root / "google_api"
        google_api.mkdir()
        (google_api / "client_baked.json").write_text(
            json.dumps(config), encoding="utf-8"
        )
    if channel == "CI-QA":
        from electridrive.build_gate import QA_MARKER_CONTENT, QA_MARKER_NAME

        (app_root / QA_MARKER_NAME).write_text(QA_MARKER_CONTENT, encoding="utf-8")
    return app_root


def test_accepts_publisher_desktop_oauth_and_picker_config():
    validate_customer_config(_client())


def test_baker_refuses_to_replace_existing_baked_oauth_file(
    monkeypatch, tmp_path: Path, capsys,
):
    from scripts import bake_client

    output = tmp_path / "client_baked.json"
    original = b'{"installed":{"client_id":"kept.apps.googleusercontent.com"}}'
    output.write_bytes(original)
    monkeypatch.setattr(bake_client, "OUT", output)

    assert bake_client.main() == 1
    assert output.read_bytes() == original
    assert "Refusing to overwrite" in capsys.readouterr().err


def test_baker_writes_private_validated_config_without_logging_values(
    monkeypatch, tmp_path: Path, capsys,
):
    from scripts import bake_client

    client_file = tmp_path / "publisher.json"
    client_file.write_text(json.dumps(_client()), encoding="utf-8")
    output = tmp_path / "client_baked.json"
    monkeypatch.setattr(bake_client, "OUT", output)
    monkeypatch.setenv("ELECTRIDRIVE_APP_CLIENT_FILE", str(client_file))
    monkeypatch.delenv("ELECTRIDRIVE_PICKER_API_KEY", raising=False)

    assert bake_client.main() == 0
    result = capsys.readouterr()

    assert output.stat().st_mode & 0o777 == 0o600
    assert json.loads(output.read_text(encoding="utf-8")) == _client()
    assert "publisher-secret" not in result.out + result.err
    assert "AIza" not in result.out + result.err


def test_placeholder_environment_bypass_is_rejected(monkeypatch, tmp_path: Path):
    from scripts import bake_client

    output = tmp_path / "client_baked.json"
    monkeypatch.setattr(bake_client, "OUT", output)
    monkeypatch.setenv("ALLOW_PLACEHOLDER_CLIENT", "1")
    monkeypatch.setenv("ELECTRIDRIVE_CLIENT_ID", "REPLACE_WITH_YOUR_CLIENT_ID")
    monkeypatch.setenv("ELECTRIDRIVE_CLIENT_SECRET", "REPLACE_WITH_YOUR_CLIENT_SECRET")
    monkeypatch.setenv("ELECTRIDRIVE_PICKER_API_KEY", "AIza" + "A" * 35)

    assert bake_client.main() == 1
    assert not output.exists()


@pytest.mark.parametrize(
    "script", ["build_deb.sh", "build_appimage.sh", "build_release.sh"]
)
def test_official_build_scripts_fail_without_publisher_config(script: str):
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    for name in (
        "ELECTRIDRIVE_APP_CLIENT_FILE",
        "ELECTRIDRIVE_APP_CLIENT_JSON",
        "ELECTRIDRIVE_CLIENT_ID",
        "ELECTRIDRIVE_CLIENT_SECRET",
        "ELECTRIDRIVE_PICKER_API_KEY",
    ):
        env.pop(name, None)
    env["ELECTRIDRIVE_BUILD_CHANNEL"] = "CUSTOMER-CANDIDATE"
    env["ALLOW_PLACEHOLDER_CLIENT"] = "1"
    env["PY"] = sys.executable
    result = subprocess.run(
        ["bash", str(root / "scripts" / script)],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "requires a publisher-provided OAuth Desktop client and Picker key" in result.stderr
    assert "PyInstaller is missing" not in result.stderr


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda data: data.pop("installed"), "Desktop installed client"),
        (lambda data: data["installed"].clear(), "client ID"),
        (
            lambda data: data["installed"].update(auth_uri="https://attacker.invalid/"),
            "authorization endpoint",
        ),
        (
            lambda data: data["installed"].update(token_uri="https://attacker.invalid/"),
            "token endpoint",
        ),
        (
            lambda data: data["installed"].update(
                auth_provider_x509_cert_url="https://attacker.invalid/certs"
            ),
            "certificate endpoint",
        ),
        (
            lambda data: data["installed"].update(
                client_id="not-a-google-desktop-client.apps.googleusercontent.com"
            ),
            "client ID",
        ),
        (lambda data: data["installed"].update(client_secret=""), "client secret"),
        (
            lambda data: data["installed"].update(redirect_uris=["https://localhost"]),
            "loopback hosts only",
        ),
        (
            lambda data: data["installed"].update(redirect_uris=["http://example.com"]),
            "loopback hosts only",
        ),
        (lambda data: data.pop("picker_api_key"), "Picker API key"),
        (
            lambda data: data.update(picker_api_key="not-a-google-key"),
            "Picker API key",
        ),
    ],
)
def test_rejects_invalid_customer_configuration(mutate, expected):
    data = _client()
    mutate(data)

    assert any(expected in error for error in customer_config_errors(data))


def test_customer_package_without_embedded_client_fails_closed(tmp_path: Path):
    _write_build(tmp_path, "CUSTOMER-CANDIDATE")

    errors = verify(tmp_path, "CUSTOMER-CANDIDATE")

    assert "customer candidate has no embedded OAuth client configuration" in errors


def test_customer_package_rejects_embedded_placeholder_config(tmp_path: Path):
    _write_build(
        tmp_path,
        "CUSTOMER-CANDIDATE",
        {"installed": {"client_id": "REPLACE_WITH_YOUR_CLIENT_ID"}},
    )

    errors = verify(tmp_path, "CUSTOMER-CANDIDATE")

    assert "embedded customer OAuth and Picker configuration is invalid" in errors


def test_customer_package_accepts_embedded_config(tmp_path: Path):
    _write_build(tmp_path, "CUSTOMER-CANDIDATE", _client())

    assert verify(tmp_path, "CUSTOMER-CANDIDATE") == []


def test_customer_package_rejects_wrong_build_provenance(tmp_path: Path):
    _write_build(tmp_path, "CUSTOMER-CANDIDATE", _client())

    errors = verify(tmp_path, "CUSTOMER-CANDIDATE", expected_revision="b" * 40)

    assert "build provenance does not match the requested source revision" in errors


def test_qa_package_is_watermarked_and_not_required_to_embed_oauth(tmp_path: Path):
    _write_build(tmp_path, "CI-QA")

    assert verify(tmp_path, "CI-QA") == []
    assert "build channel does not match the requested channel" in verify(
        tmp_path, "CUSTOMER-CANDIDATE"
    )


def test_qa_package_rejects_embedded_publisher_client(tmp_path: Path):
    _write_build(tmp_path, "CI-QA", {})

    assert "CI-QA artifact must not include publisher OAuth configuration" in verify(
        tmp_path, "CI-QA"
    )


def test_direct_pyinstaller_gate_rejects_missing_channel_and_provenance(tmp_path: Path):
    from electridrive.build_gate import validate_build_inputs

    assert validate_build_inputs(tmp_path) == [
        "build channel metadata is missing or unreadable"
    ]


def test_direct_pyinstaller_spec_cannot_be_bypassed_by_build_channel_env(monkeypatch):
    import runpy

    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("ELECTRIDRIVE_BUILD_CHANNEL", "CUSTOMER-CANDIDATE")
    with pytest.raises(SystemExit, match="PyInstaller release gate rejected"):
        runpy.run_path(
            str(root / "packaging" / "electridrive.spec"),
            init_globals={"SPECPATH": str(root / "packaging")},
        )


def test_qa_build_metadata_contains_machine_checked_marker(monkeypatch, tmp_path: Path):
    from types import SimpleNamespace

    from electridrive.build_gate import QA_CHANNEL, validate_build_inputs
    from scripts import prepare_build_metadata

    package_dir = tmp_path / "electridrive"
    (package_dir / "google_api").mkdir(parents=True)
    monkeypatch.setattr(prepare_build_metadata, "ROOT", tmp_path)
    monkeypatch.setattr(prepare_build_metadata, "PACKAGE_DIR", package_dir)
    monkeypatch.setenv("ELECTRIDRIVE_BUILD_CHANNEL", QA_CHANNEL)
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    output = iter((_REVISION + "\n", ""))
    monkeypatch.setattr(
        prepare_build_metadata.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=next(output)),
    )

    assert prepare_build_metadata.main() == 0
    assert validate_build_inputs(package_dir) == []


def test_customer_package_has_priority_over_bad_existing_install(
    monkeypatch, tmp_path: Path,
):
    from electridrive.google_api import app_client

    config_dir = tmp_path / "config" / "electridrive"
    config_dir.mkdir(parents=True)
    (config_dir / "credentials.json").write_text(
        '{"installed":{"client_id":"REPLACE_WITH_YOUR_CLIENT_ID"}}',
        encoding="utf-8",
    )
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    package_client = package_dir / "client_baked.json"
    package_client.write_text(json.dumps(_client()), encoding="utf-8")
    monkeypatch.setattr(app_client, "build_channel", lambda: "CUSTOMER-CANDIDATE")
    monkeypatch.setattr(app_client, "_PACKAGE_FILE", package_client)
    class Paths:
        credentials_file = config_dir / "credentials.json"

    monkeypatch.setattr(app_client, "get_paths", Paths)

    assert app_client.client_id() == _client()["installed"]["client_id"]
    assert app_client.picker_api_key() == _client()["picker_api_key"]


@pytest.mark.parametrize(
    ("channel", "expected"),
    [
        ("CI-QA", "ElectriDrive — CI-QA — NOT FOR CUSTOMERS"),
        ("CUSTOMER-CANDIDATE", "ElectriDrive — CUSTOMER CANDIDATE — NOT RELEASED"),
    ],
)
def test_window_title_displays_build_watermark(monkeypatch, tmp_path: Path, channel, expected):
    from electridrive import build_channel

    (tmp_path / "build_channel.txt").write_text(channel, encoding="utf-8")
    monkeypatch.setattr(build_channel, "files", lambda _package: tmp_path)

    assert build_channel.window_title() == expected


def test_client_discovery_does_not_change_active_settings(monkeypatch, tmp_path: Path):
    from electridrive.google_api import app_client

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("ELECTRIDRIVE_CLIENT_ID", raising=False)
    config_dir = tmp_path / "electridrive"
    config_dir.mkdir()
    settings = config_dir / "settings.json"
    settings.write_text(
        '{"scope":"drive","default_remote_folder":"active","sync_pairs":[]}',
        encoding="utf-8",
    )
    (config_dir / "app_client.json").write_text(
        json.dumps({"installed": {"client_id": "incomplete.apps.googleusercontent.com"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(app_client, "_PACKAGE_FILE", tmp_path / "no-package-client.json")
    before = settings.read_bytes()

    app_client.client_config()
    customer_config_errors(json.loads((config_dir / "app_client.json").read_text()))

    assert settings.read_bytes() == before


def test_login_view_shows_safe_offline_setup_copy(monkeypatch, tmp_path: Path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from types import SimpleNamespace

    from PySide6.QtWidgets import QApplication

    from electridrive.google_api import app_client
    from electridrive.ui.theme import get_palette
    from electridrive.ui.views.login_view import LoginView

    app = QApplication.instance() or QApplication([])
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("ELECTRIDRIVE_CLIENT_ID", raising=False)
    monkeypatch.setattr(app_client, "_PACKAGE_FILE", tmp_path / "missing-package-client.json")
    session = SimpleNamespace(
        has_credentials=lambda: False,
        paths=SimpleNamespace(config_dir=tmp_path / "electridrive"),
    )
    view = LoginView(session, get_palette("electric_dark"))
    try:
        message = view.status.text()
        assert "connection is unavailable" in message
        assert "Contact the publisher" in message
        assert not view.connect_btn.isEnabled()
        assert "publisher-secret" not in message
    finally:
        view.close()
        app.processEvents()


def test_picker_fails_with_clear_error_when_api_key_is_missing(monkeypatch):
    from electridrive.google_api import app_client, picker

    monkeypatch.setattr(app_client, "is_configured", lambda: True)
    monkeypatch.setattr(app_client, "is_picker_configured", lambda: False)

    with pytest.raises(picker.PickerError, match="Google Picker API key"):
        picker.pick_from_drive()
