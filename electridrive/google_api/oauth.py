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
        from keyring.errors import NoKeyringError
    except ImportError:
        return None
    try:
        return keyring.get_password(SERVICE_NAME, TOKEN_KEY)
    except NoKeyringError:
        return None
    except Exception as exc:  # pragma: no cover - depends on desktop env
        raise OAuthError("Could not access the credential keyring; unlock it and retry.") from exc


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


def _parse_token_json(raw: str | bytes) -> dict[str, Any]:
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise OAuthError("Invalid saved token metadata.")
    return data


def _load_token_from_file(path: Path) -> dict[str, Any] | None:
    policy = current_policy()
    if policy:
        policy.verify()
        if path != get_paths().token_fallback_file:
            raise OAuthError("QA token path mismatch")
        check_profile_file(path, strict=True)
        return _parse_token_json(_read(path)) if path.exists() else None
    if not path.exists():
        return None
    try:
        return _parse_token_json(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise OAuthError("Could not read saved token file.") from exc


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

    # The latest fallback remains authoritative until keyring rotation succeeds.
    token_data = _load_token_from_file(paths.token_fallback_file)
    if token_data is None:
        token_json = _try_keyring_get()
        try:
            token_data = json.loads(token_json) if token_json else None
        except (ValueError, TypeError) as exc:
            raise OAuthError("Could not read saved keyring token.") from exc
    if token_data is None:
        return None
    if not isinstance(token_data, dict):
        raise OAuthError("Invalid saved token metadata.")
    if _scope_set(token_data.get("scopes")) != set(scopes):
        if current_policy():
            raise OAuthError("QA requires a separately authorized drive.file token")
        clear_credentials()
        return None
    try:
        return Credentials.from_authorized_user_info(token_data)
    except (ValueError, TypeError) as exc:
        raise OAuthError("Invalid saved token metadata.") from exc


def _scope_set(scopes) -> set[str]:
    if isinstance(scopes, str):
        scopes = scopes.split()
    return set(scopes or [])


def validate_scopes(creds, scopes: list[str]) -> None:
    """Require actual grants to match; requested labels cannot narrow a token."""
    granted = getattr(creds, "granted_scopes", None)
    actual = granted if granted is not None else getattr(creds, "scopes", None)
    if not actual or _scope_set(actual) != set(scopes):
        raise OAuthError(
            "Google granted different scopes than requested. Revoke this app's existing "
            "access in your Google account and reconnect with the selected scope."
        )


def has_saved_token() -> bool:
    """Check checked profile storage in QA; fallback or keyring in normal mode."""
    data = _load_token_from_file(get_paths().token_fallback_file)
    if data is not None:
        return True
    return False if current_policy() else bool(_try_keyring_get())


def save_credentials(creds) -> None:
    paths = get_paths()
    token_data = json.loads(creds.to_json())
    granted = getattr(creds, "granted_scopes", None)
    if granted is not None:
        token_data["scopes"] = sorted(_scope_set(granted))
    token_json = json.dumps(token_data)
    if _try_keyring_set(token_json):
        paths.token_fallback_file.unlink(missing_ok=True)
    else:
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
        validate_scopes(creds, scopes)
        return creds

    if creds and creds.expired and creds.refresh_token:
        from google.auth.exceptions import RefreshError
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            details = next((arg for arg in exc.args if isinstance(arg, dict)), {})
            if details.get("error") != "invalid_grant" and not str(exc).startswith("invalid_grant:"):
                raise
            clear_credentials()
        else:
            validate_scopes(creds, scopes)
            save_credentials(creds)
            return creds

    from electridrive.google_api import app_client

    if not app_client.is_configured():
        raise OAuthError(
            "No OAuth client configured. Set ELECTRIDRIVE_CLIENT_ID or provide "
            f"a Desktop OAuth client at {paths.credentials_file}."
        )
    flow = InstalledAppFlow.from_client_config(app_client.client_config(), scopes)
    # run_local_server uses a loopback redirect with PKCE (S256).
    creds = flow.run_local_server(port=0)
    validate_scopes(creds, scopes)
    save_credentials(creds)
    return creds
