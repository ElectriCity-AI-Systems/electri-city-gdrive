from __future__ import annotations

import re
from urllib.parse import urlparse
from typing import Any


_CLIENT_ID = re.compile(r"^[A-Za-z0-9._-]+\.apps\.googleusercontent\.com$")
_PICKER_KEY = re.compile(r"^AIza[0-9A-Za-z_-]{35}$")
_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
_TOKEN_URI = "https://oauth2.googleapis.com/token"
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def customer_config_errors(data: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["OAuth client JSON must be an object"]

    client = data.get("installed")
    if not isinstance(client, dict):
        errors.append("OAuth configuration must contain a Desktop installed client")
        client = {}

    client_id = client.get("client_id")
    if not isinstance(client_id, str) or not _CLIENT_ID.fullmatch(client_id):
        errors.append("OAuth Desktop client ID is missing or malformed")
    elif "REPLACE_WITH_YOUR_" in client_id:
        errors.append("OAuth Desktop client ID is a placeholder")

    secret = client.get("client_secret")
    if not isinstance(secret, str) or not secret or "REPLACE_WITH_YOUR_" in secret:
        errors.append("OAuth Desktop client secret is missing or a placeholder")

    if client.get("auth_uri") != _AUTH_URI:
        errors.append("OAuth authorization endpoint is not Google's expected endpoint")
    if client.get("token_uri") != _TOKEN_URI:
        errors.append("OAuth token endpoint is not Google's expected endpoint")

    redirects = client.get("redirect_uris")
    if not isinstance(redirects, list) or not redirects:
        errors.append("OAuth Desktop client has no loopback redirect URI")
    elif not all(_is_safe_loopback_redirect(uri) for uri in redirects):
        errors.append("OAuth redirect URIs must use HTTP loopback hosts only")

    picker_key = data.get("picker_api_key")
    if not isinstance(picker_key, str) or not _PICKER_KEY.fullmatch(picker_key):
        errors.append("Google Picker API key is missing or malformed")
    elif "REPLACE_WITH_YOUR_" in picker_key:
        errors.append("Google Picker API key is a placeholder")
    return errors


def _is_safe_loopback_redirect(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlparse(value)
        return (
            parsed.scheme == "http"
            and parsed.hostname in _LOOPBACK_HOSTS
            and parsed.username is None
            and parsed.password is None
            and parsed.path in ("", "/")
            and not parsed.query
            and not parsed.fragment
            and (parsed.port is None or 1 <= parsed.port <= 65535)
        )
    except ValueError:
        return False


def validate_customer_config(data: Any) -> None:
    errors = customer_config_errors(data)
    if errors:
        raise ValueError("; ".join(errors))
