from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from electridrive.google_api.release_config import (
    customer_config_errors,
    validate_customer_config,
)
from scripts.verify_customer_artifact import verify


def _client() -> dict:
    return {
        "installed": {
            "client_id": "publisher.apps.googleusercontent.com",
            "client_secret": "publisher-secret",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        },
        "picker_api_key": "AIza" + "A" * 35,
    }


def test_accepts_publisher_desktop_oauth_and_picker_config():
    validate_customer_config(_client())


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda data: data.pop("installed"), "Desktop installed client"),
        (
            lambda data: data["installed"].update(auth_uri="https://attacker.invalid/"),
            "authorization endpoint",
        ),
        (
            lambda data: data["installed"].update(token_uri="https://attacker.invalid/"),
            "token endpoint",
        ),
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
    (tmp_path / "build_channel.txt").write_text("CUSTOMER-CANDIDATE\n", encoding="utf-8")

    errors = verify(tmp_path, "CUSTOMER-CANDIDATE")

    assert "customer candidate has no embedded OAuth client configuration" in errors


def test_customer_package_rejects_embedded_placeholder_config(tmp_path: Path):
    (tmp_path / "build_channel.txt").write_text("CUSTOMER-CANDIDATE\n", encoding="utf-8")
    (tmp_path / "client_baked.json").write_text(
        json.dumps({"installed": {"client_id": "REPLACE_WITH_YOUR_CLIENT_ID"}}),
        encoding="utf-8",
    )

    errors = verify(tmp_path, "CUSTOMER-CANDIDATE")

    assert "embedded customer OAuth and Picker configuration is invalid" in errors


def test_customer_package_accepts_embedded_config(tmp_path: Path):
    (tmp_path / "build_channel.txt").write_text("CUSTOMER-CANDIDATE\n", encoding="utf-8")
    (tmp_path / "client_baked.json").write_text(
        json.dumps(_client()), encoding="utf-8"
    )

    assert verify(tmp_path, "CUSTOMER-CANDIDATE") == []


def test_qa_package_is_watermarked_and_not_required_to_embed_oauth(tmp_path: Path):
    (tmp_path / "build_channel.txt").write_text("CI-QA\n", encoding="utf-8")

    assert verify(tmp_path, "CI-QA") == []
    assert "artifact build-channel watermark does not match expected channel" in verify(
        tmp_path, "CUSTOMER-CANDIDATE"
    )


def test_qa_package_rejects_embedded_publisher_client(tmp_path: Path):
    (tmp_path / "build_channel.txt").write_text("CI-QA\n", encoding="utf-8")
    (tmp_path / "client_baked.json").write_text("{}", encoding="utf-8")

    assert "CI-QA artifact must not include publisher OAuth configuration" in verify(
        tmp_path, "CI-QA"
    )


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
