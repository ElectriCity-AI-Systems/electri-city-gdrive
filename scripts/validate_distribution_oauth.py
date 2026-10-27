"""Fail-closed validation of OAuth configuration for customer distributions.

A source checkout intentionally has no Google credentials. Public customer
packages MUST embed a working-shaped Desktop OAuth client and Picker API key.
Offline checks do not replace an actual account sign-in before publication.

Never print a client secret, client ID, or API key.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit


CLIENT_ID = re.compile(r"^[0-9]+-[A-Za-z0-9_-]+\.apps\.googleusercontent\.com$")
PICKER_KEY = re.compile(r"^AIza[A-Za-z0-9_-]{30,}$")
PLACEHOLDERS = ("REPLACE_WITH_YOUR", "PLACEHOLDER", "EXAMPLE", "YOUR_", "TODO")


def validate_client_data(data: object) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["OAuth client JSON must be an object"]
    client = data.get("installed")
    if not isinstance(client, dict):
        return ["OAuth configuration must contain an installed Desktop client"]
    cid = client.get("client_id")
    if not isinstance(cid, str) or not CLIENT_ID.fullmatch(cid.strip()):
        errors.append("invalid or missing Google Desktop client_id")
    elif any(marker in cid.upper() for marker in PLACEHOLDERS):
        errors.append("placeholder Google Desktop client_id")
    secret = client.get("client_secret")
    if not isinstance(secret, str) or not secret.strip() or any(
        marker in secret.upper() for marker in PLACEHOLDERS
    ):
        errors.append("invalid or missing Desktop client_secret")
    if client.get("auth_uri") != "https://accounts.google.com/o/oauth2/auth":
        errors.append("unsupported Google authorization endpoint")
    if client.get("token_uri") != "https://oauth2.googleapis.com/token":
        errors.append("unsupported Google token endpoint")
    redirects = client.get("redirect_uris", [])
    if not isinstance(redirects, list) or not any(
        isinstance(value, str)
        and urlsplit(value).scheme == "http"
        and urlsplit(value).hostname in {"localhost", "127.0.0.1"}
        for value in redirects
    ):
        errors.append("Desktop loopback redirect URI missing")
    picker = data.get("picker_api_key")
    if not isinstance(picker, str) or not PICKER_KEY.fullmatch(picker.strip()):
        errors.append("missing or invalid Google Picker API key")
    elif any(marker in picker.upper() for marker in PLACEHOLDERS):
        errors.append("placeholder Google Picker API key")
    return errors


def validate_client_file(path: Path) -> list[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return ["missing or invalid OAuth client JSON"]
    return validate_client_data(data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    args = parser.parse_args(argv)
    errors = validate_client_file(args.file)
    if errors:
        for error in errors:
            print(f"RELEASE BLOCKED: {error}", file=sys.stderr)
        return 1
    print("OAuth client + Picker key validated (values not displayed).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
