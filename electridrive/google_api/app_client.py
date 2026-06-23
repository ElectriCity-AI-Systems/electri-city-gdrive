"""Built-in OAuth client configuration for global distribution.

A desktop app is a *public* OAuth client: the client_id and client_secret are not
confidential and ship inside the app. End users just sign in — no per-user
``credentials.json``. Authorization uses PKCE + a loopback redirect.

Resolution order (first match wins):
1. ``$ELECTRIDRIVE_CLIENT_ID`` / ``$ELECTRIDRIVE_CLIENT_SECRET`` env vars
2. ``<config_dir>/app_client.json`` (an installed-app client JSON)
3. the bundled :data:`BUNDLED_CLIENT` below

A user-provided ``<config_dir>/credentials.json`` (handled in ``oauth.py``) still
takes precedence over all of these for power users / self-hosting.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from electridrive.config import get_paths

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
    return _read_json(get_paths().config_dir / "app_client.json")


def _package_data() -> dict[str, Any] | None:
    return _read_json(_PACKAGE_FILE)


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
    """OAuth client config. Precedence: env > user file > baked package > placeholder."""
    return (_from_env() or _config_from(_user_file_data())
            or _config_from(_package_data()) or BUNDLED_CLIENT)


def client_id() -> str:
    section = client_config().get("installed") or client_config().get("web") or {}
    return section.get("client_id", "")


def picker_api_key() -> str:
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
