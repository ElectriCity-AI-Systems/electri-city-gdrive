"""Built-in OAuth client configuration for global distribution.

A desktop app is a *public* OAuth client: the client_id and client_secret are not
confidential and ship inside the app. End users just sign in — no per-user
``credentials.json``. Authorization uses PKCE + a loopback redirect.

Customer candidate packages use their validated embedded publisher client and
Picker key before any profile files or environment overrides. Other builds use
profile/environment client configuration and then the placeholder below.

The same resolver is used by desktop login and Picker. QA mode permits only
checked profile client files and the unconfigured placeholder.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from electridrive.config import get_paths
from electridrive.qa_policy import current_policy, _read
from electridrive.build_channel import build_channel

_PLACEHOLDER = "REPLACE_WITH_YOUR_"
# Build-time baked client, shipped inside the package (written by scripts/bake_client.py;
# gitignored). Lets distributed builds carry the OAuth client without a per-user file.
_PACKAGE_FILE = Path(__file__).resolve().parent / "client_baked.json"

# Fill these in (or ship a build with them filled) to enable one-click sign-in.
# Create an OAuth client of type "Desktop app" in Google Cloud, enable the Drive API
# and the Picker API, and paste the values here or into <config_dir>/app_client.json.
BUNDLED_CLIENT: dict[str, Any] = {
    "installed": {
        "client_id": f"{_PLACEHOLDER}CLIENT_ID.apps.googleusercontent.com",
        "client_secret": f"{_PLACEHOLDER}CLIENT_SECRET",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "redirect_uris": ["http://localhost"],
    }
}

# Browser API key for the Google Picker (separate from the OAuth client).
BUNDLED_PICKER_API_KEY = f"{_PLACEHOLDER}PICKER_API_KEY"


def _from_env() -> dict[str, Any] | None:
    cid = os.environ.get("ELECTRIDRIVE_CLIENT_ID")
    if not cid:
        return None
    return {
        "installed": {
            "client_id": cid,
            "client_secret": os.environ.get("ELECTRIDRIVE_CLIENT_SECRET", ""),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "redirect_uris": ["http://localhost"],
        }
    }


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _user_file_data() -> dict[str, Any] | None:
    path = get_paths().config_dir / "app_client.json"
    if current_policy():
        return json.loads(_read(path)) if path.exists() or path.is_symlink() else None
    return _read_json(path)


def _package_data() -> dict[str, Any] | None:
    return _read_json(_PACKAGE_FILE)


def _is_customer_package() -> bool:
    return not current_policy() and build_channel() == "CUSTOMER-CANDIDATE"


def _config_from(data: dict[str, Any] | None) -> dict[str, Any] | None:
    if not data:
        return None
    if "installed" in data:
        return {"installed": data["installed"]}
    if "web" in data:
        return {"web": data["web"]}
    bare = {k: v for k, v in data.items() if k != "picker_api_key"}
    return {"installed": bare} if bare.get("client_id") else None


def client_config() -> dict[str, Any]:
    """Use one selected Desktop client for both login and Picker."""
    if _is_customer_package():
        data = _package_data()
        if data is None:
            raise ValueError("Publisher-configured OAuth client is unavailable in this package.")
        from electridrive.google_api.release_config import validate_customer_config

        validate_customer_config(data)
        config = _config_from(data)
        if config is None:
            raise ValueError("Publisher-configured OAuth client is invalid.")
        return config
    path = get_paths().credentials_file
    if path.exists() or path.is_symlink():
        # _read preserves QA profile confinement, including dangling symlinks.
        data = json.loads(_read(path)) if current_policy() else json.loads(path.read_text(encoding="utf-8"))
        config = _config_from(data)
        if not config:
            raise ValueError("Invalid Desktop OAuth client configuration")
        return config
    if current_policy():
        return _config_from(_user_file_data()) or BUNDLED_CLIENT
    return (_from_env() or _config_from(_user_file_data())
            or _config_from(_package_data()) or BUNDLED_CLIENT)


def client_id() -> str:
    section = client_config().get("installed") or client_config().get("web") or {}
    return section.get("client_id", "")


def picker_api_key() -> str:
    if current_policy():
        data = _user_file_data() or {}
        return str(data.get("picker_api_key", BUNDLED_PICKER_API_KEY))
    if _is_customer_package():
        data = _package_data() or {}
        return str(data.get("picker_api_key", BUNDLED_PICKER_API_KEY))
    env = os.environ.get("ELECTRIDRIVE_PICKER_API_KEY")
    if env:
        return env
    for data in (_user_file_data(), _package_data()):
        if data and data.get("picker_api_key"):
            return str(data["picker_api_key"])
    return BUNDLED_PICKER_API_KEY


def is_configured() -> bool:
    """True once a real (non-placeholder) OAuth client is available."""
    return bool(client_id()) and _PLACEHOLDER not in client_id()


def is_picker_configured() -> bool:
    return _PLACEHOLDER not in picker_api_key()
