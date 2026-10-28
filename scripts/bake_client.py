"""Bake the built-in OAuth client into the package for distribution.

Reads the client from the environment and writes
``electridrive/google_api/client_baked.json`` (gitignored), which the shipped app
loads so end users never need their own credentials.json.

Usage:
    ELECTRIDRIVE_CLIENT_ID=... ELECTRIDRIVE_CLIENT_SECRET=... \\
    ELECTRIDRIVE_PICKER_API_KEY=... python scripts/bake_client.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from electridrive.google_api.release_config import validate_customer_config

OUT = Path(__file__).resolve().parents[1] / "electridrive" / "google_api" / "client_baked.json"


def main() -> int:
    client_file = os.environ.get("ELECTRIDRIVE_APP_CLIENT_FILE")
    if client_file:
        try:
            data = json.loads(Path(client_file).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            print("OAuth Desktop client JSON could not be read.", file=sys.stderr)
            return 1
    else:
        cid = os.environ.get("ELECTRIDRIVE_CLIENT_ID")
        if not cid:
            print(
                "Set a publisher-provided Desktop client file or "
                "ELECTRIDRIVE_CLIENT_ID and ELECTRIDRIVE_CLIENT_SECRET.",
                file=sys.stderr,
            )
            return 1
        data = {
            "installed": {
                "client_id": cid,
                "client_secret": os.environ.get("ELECTRIDRIVE_CLIENT_SECRET", ""),
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
                "redirect_uris": ["http://localhost"],
            }
        }
    picker = os.environ.get("ELECTRIDRIVE_PICKER_API_KEY")
    if picker:
        data["picker_api_key"] = picker
    try:
        validate_customer_config(data)
    except ValueError as exc:
        print(f"Customer OAuth configuration rejected: {exc}", file=sys.stderr)
        return 1
    OUT.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print("Baked validated customer OAuth and Picker configuration.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
