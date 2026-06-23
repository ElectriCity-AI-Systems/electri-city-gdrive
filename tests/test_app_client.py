import json
from pathlib import Path

import pytest

from electridrive.google_api import app_client


def test_bundled_is_unconfigured_placeholder(monkeypatch, tmp_path):
    monkeypatch.delenv("ELECTRIDRIVE_CLIENT_ID", raising=False)
    # empty config dir + no baked package file -> falls back to the bundled placeholder
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(app_client, "_PACKAGE_FILE", tmp_path / "no_baked.json")
    assert app_client.client_id().startswith("REPLACE_WITH_")
    assert app_client.is_configured() is False


def test_env_override_configures(monkeypatch):
    monkeypatch.setenv("ELECTRIDRIVE_CLIENT_ID", "abc123.apps.googleusercontent.com")
    monkeypatch.setenv("ELECTRIDRIVE_CLIENT_SECRET", "s3cr3t")
    assert app_client.client_id() == "abc123.apps.googleusercontent.com"
    assert app_client.is_configured() is True
    assert app_client.client_config()["installed"]["client_secret"] == "s3cr3t"


def test_file_config_used_when_no_env(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("ELECTRIDRIVE_CLIENT_ID", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    cfg_dir = tmp_path / "electridrive"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "app_client.json").write_text(
        json.dumps({"installed": {"client_id": "fromfile.apps.googleusercontent.com"}}),
        encoding="utf-8")
    assert app_client.client_id() == "fromfile.apps.googleusercontent.com"
    assert app_client.is_configured() is True


def test_env_takes_precedence_over_file(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    cfg_dir = tmp_path / "electridrive"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "app_client.json").write_text(
        json.dumps({"installed": {"client_id": "fromfile.apps.googleusercontent.com"}}),
        encoding="utf-8")
    monkeypatch.setenv("ELECTRIDRIVE_CLIENT_ID", "fromenv.apps.googleusercontent.com")
    assert app_client.client_id() == "fromenv.apps.googleusercontent.com"
