"""Customer-distribution gate regression tests; no actual Google credentials."""
from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.validate_distribution_oauth import (
    validate_client_data,
    validate_client_file,
)
from scripts.verify_deb import _verify_baked_client


def valid_fixture() -> dict:
    return {
        "installed": {
            "client_id": "123456789-abcDEF123.apps.googleusercontent.com",
            "client_secret": "GOCSPX-fixture-not-a-real-secret",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        },
        "picker_api_key": "AIza" + "x" * 35,
    }


def test_complete_customer_client_is_accepted_without_live_oauth():
    assert validate_client_data(valid_fixture()) == []


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("client_id", "REPLACE_WITH_YOUR_CLIENT_ID.apps.googleusercontent.com", "client_id"),
        ("client_id", "fake-id", "client_id"),
        ("client_secret", "", "client_secret"),
        ("client_secret", "REPLACE_WITH_YOUR_CLIENT_SECRET", "client_secret"),
        ("auth_uri", "https://evil.invalid/authorize", "authorization"),
        ("token_uri", "https://evil.invalid/token", "token"),
        ("redirect_uris", ["https://example.com/redirect"], "loopback"),
    ],
)
def test_release_rejects_invalid_desktop_configuration(field, value, message):
    data = valid_fixture()
    data["installed"][field] = value
    assert any(message in e for e in validate_client_data(data))


def test_release_rejects_missing_picker_key():
    data = valid_fixture()
    del data["picker_api_key"]
    assert any("Picker" in e for e in validate_client_data(data))


def test_release_rejects_web_client_only():
    data = valid_fixture()
    data["web"] = data.pop("installed")
    assert any("Desktop" in e for e in validate_client_data(data))


def test_release_rejects_missing_or_malformed_client_file(tmp_path: Path):
    path = tmp_path / "client.json"
    assert validate_client_file(path)
    path.write_text("{", encoding="utf-8")
    assert validate_client_file(path)


def test_cli_never_logs_credentials(tmp_path: Path):
    path = tmp_path / "client.json"
    data = valid_fixture()
    data["picker_api_key"] = "INVALID-" + "REAL_TOKEN_MUST_NOT_LEAK"
    path.write_text(json.dumps(data), encoding="utf-8")
    tool = Path(__file__).resolve().parents[1] / "scripts/validate_distribution_oauth.py"
    result = subprocess.run(
        [sys.executable, str(tool), str(path)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0
    assert "RELEASE BLOCKED" in result.stderr
    assert data["installed"]["client_secret"] not in result.stderr
    assert data["picker_api_key"] not in result.stderr
    assert data["installed"]["client_id"] not in result.stderr


def test_package_verifier_blocks_missing_client(tmp_path: Path):
    errors, _ = _verify_baked_client(tmp_path)
    assert errors


def test_package_verifier_blocks_explicit_qa_marker_even_with_valid_client(tmp_path: Path):
    (tmp_path / "QA-NOT-FOR-DISTRIBUTION.txt").write_text("CI TEST")
    path = tmp_path / "client_baked.json"
    path.write_text(json.dumps(valid_fixture()), encoding="utf-8")
    errors, _ = _verify_baked_client(tmp_path)
    assert any("QA-only" in e for e in errors)


def test_package_verifier_blocks_malformed_client(tmp_path: Path):
    path = tmp_path / "client_baked.json"
    data = copy.deepcopy(valid_fixture())
    data["picker_api_key"] = "REPLACE_WITH_YOUR_PICKER_KEY"
    path.write_text(json.dumps(data), encoding="utf-8")
    errors, _ = _verify_baked_client(tmp_path)
    assert any("Picker" in e for e in errors)


def test_package_verifier_accepts_valid_structure(tmp_path: Path):
    path = tmp_path / "client_baked.json"
    path.write_text(json.dumps(valid_fixture()), encoding="utf-8")
    assert _verify_baked_client(tmp_path)[0] == []


def test_distribution_builds_explicitly_gate_oauth_and_ci_placeholder():
    root = Path(__file__).resolve().parents[1]
    deb = (root / "scripts/build_deb.sh").read_text(encoding="utf-8")
    appimage = (root / "scripts/build_appimage.sh").read_text(encoding="utf-8")
    docker = (root / "scripts/build_deb_in_container.sh").read_text(encoding="utf-8")
    for script in (deb, appimage):
        assert "validate_distribution_oauth.py" in script
        assert "RELEASE BLOCKED" in script
    assert "QA-NOT-FOR-DISTRIBUTION.txt" in deb
    assert "ALLOW_PLACEHOLDER_CLIENT=1" in docker
