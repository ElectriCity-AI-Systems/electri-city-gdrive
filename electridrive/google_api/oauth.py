from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from electridrive.config import get_paths, selected_scopes
from electridrive.qa_policy import current_policy, check_profile_file, _read

LOGGER = logging.getLogger(__name__)
SERVICE_NAME = "ElectriDrive Linux"
TOKEN_KEY = "google_oauth_token_json"


class OAuthError(RuntimeError):
    pass


def _try_keyring_get() -> str | None:
    if current_policy():
        return None
    try:
        import keyring
        return keyring.get_password(SERVICE_NAME, TOKEN_KEY)
    except Exception as exc:  # pragma: no cover - depends on desktop env
        LOGGER.debug("keyring get unavailable: %s", exc)
        return None


def _try_keyring_set(token_json: str) -> bool:
    if current_policy():
        return False
    try:
        import keyring
        keyring.set_password(SERVICE_NAME, TOKEN_KEY, token_json)
        return True
    except Exception as exc:  # pragma: no cover - depends on desktop env
        LOGGER.warning("keyring unavailable, falling back to token file: %s", exc)
        return False


def _load_token_from_file(path: Path) -> dict[str, Any] | None:
    policy = current_policy()
    if policy:
        policy.verify()
        if path != get_paths().token_fallback_file:
            raise OAuthError("QA token path mismatch")
        check_profile_file(path, strict=True)
        return json.loads(_read(path)) if path.exists() else None
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        LOGGER.warning("Could not read token fallback file: %s", exc)
        return None


def _save_token_to_file(path: Path, token_json: str) -> None:
    policy = current_policy()
    if policy:
        policy.verify()
        if path != get_paths().token_fallback_file:
            raise OAuthError("QA token path mismatch")
        check_profile_file(path, strict=True)
        from electridrive.qa_policy import _write
        _write(path, json.loads(token_json))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token_json, encoding="utf-8")
    path.chmod(0o600)


def load_credentials():
    try:
        from google.oauth2.credentials import Credentials
    except Exception as exc:  # pragma: no cover
        raise OAuthError("Google auth dependencies are not installed. Run: pip install -r requirements.txt") from exc

    paths = get_paths()
    scopes = selected_scopes()

    token_json = _try_keyring_get()
    token_data = json.loads(token_json) if token_json else _load_token_from_file(paths.token_fallback_file)
    if not token_data:
        return None
    if current_policy():
        stored_scopes = token_data.get("scopes", [])
        if isinstance(stored_scopes, str):
            stored_scopes = stored_scopes.split()
        if not stored_scopes or set(stored_scopes) - set(scopes):
            raise OAuthError("QA requires a separately authorized drive.file token")
    return Credentials.from_authorized_user_info(token_data, scopes)


def save_credentials(creds) -> None:
    paths = get_paths()
    token_json = creds.to_json()
    if not _try_keyring_set(token_json):
        _save_token_to_file(paths.token_fallback_file, token_json)


def clear_credentials() -> None:
    """Forget the saved token (keyring entry + fallback file). Used on sign-out and
    when switching access mode so the next connect re-consents with the new scope."""
    if current_policy():
        path = get_paths().token_fallback_file
        check_profile_file(path, strict=True)
        path.unlink(missing_ok=True)
        return
    try:
        import keyring
        keyring.delete_password(SERVICE_NAME, TOKEN_KEY)
    except Exception as exc:  # pragma: no cover - depends on desktop env
        LOGGER.debug("keyring delete unavailable: %s", exc)
    try:
        get_paths().token_fallback_file.unlink(missing_ok=True)
    except Exception as exc:
        LOGGER.debug("token file delete failed: %s", exc)


def authenticate_interactive():
    try:
        from google.auth.transport.requests import Request
        from google_auth_oauthlib.flow import InstalledAppFlow
    except Exception as exc:  # pragma: no cover
        raise OAuthError("Google OAuth dependencies are not installed. Run: pip install -r requirements.txt") from exc

    paths = get_paths()
    scopes = selected_scopes()
    creds = load_credentials()

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        save_credentials(creds)
        return creds

    from electridrive.google_api import app_client

    if paths.credentials_file.exists() or paths.credentials_file.is_symlink():
        # Power-user / self-host: a user-supplied Desktop OAuth client takes precedence.
        if current_policy():
            flow = InstalledAppFlow.from_client_config(json.loads(_read(paths.credentials_file)), scopes)
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(paths.credentials_file), scopes)
    elif app_client.is_configured():
        # Global distribution: the built-in app client — no per-user credentials.json.
        flow = InstalledAppFlow.from_client_config(app_client.client_config(), scopes)
    else:
        raise OAuthError(
            "No OAuth client configured. Set the built-in app client via "
            "ELECTRIDRIVE_CLIENT_ID (and ELECTRIDRIVE_CLIENT_SECRET) or "
            f"{paths.config_dir / 'app_client.json'}, or drop your own Desktop OAuth "
            f"client at {paths.credentials_file}."
        )
    # run_local_server uses a loopback redirect with PKCE (S256).
    creds = flow.run_local_server(port=0)
    save_credentials(creds)
    return creds
