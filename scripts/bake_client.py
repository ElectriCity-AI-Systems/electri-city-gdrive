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

OUT = Path(__file__).resolve().parents[1] / "electridrive" / "google_api" / "client_baked.json"


def main() -> int:
    cid = os.environ.get("ELECTRIDRIVE_CLIENT_ID")
    if not cid:
        print("Set ELECTRIDRIVE_CLIENT_ID (and _CLIENT_SECRET, _PICKER_API_KEY) first.",
              file=sys.stderr)
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
    OUT.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"Baked client to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
