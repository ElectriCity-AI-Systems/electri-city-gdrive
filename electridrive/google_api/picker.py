"""Desktop Google Picker flow for the ``drive.file`` scope.

Lets the user grant the app access to specific existing Drive files/folders without
any restricted scope. Per Google's desktop Picker guide, the app starts an OAuth
request with ``trigger_onepick=true``; Google opens the Picker in the user's browser
and redirects back to our loopback server with ``code`` and ``picked_file_ids``.

The parameter/parsing helpers are pure and unit-tested; the orchestration opens a
browser and needs a configured app client + the Picker API enabled.
"""
from __future__ import annotations

import logging
import os
import time
import webbrowser

# Google may return a broader scope than requested if the user already granted one to
# this client (e.g. full drive). Relax oauthlib's strict scope-equality check so the
# token exchange doesn't raise; onepick still requests only drive.file.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from electridrive.config import DEFAULT_SCOPE
from electridrive.google_api.oauth import save_credentials

LOGGER = logging.getLogger(__name__)


class PickerError(RuntimeError):
    pass


@dataclass
class PickResult:
    credentials: object
    file_ids: list[str] = field(default_factory=list)


def picker_auth_params(allow_folders: bool = True) -> dict[str, str]:
    """Extra authorization-URL params that turn the consent flow into a Picker.

    NOTE: the onepick flow permits ONLY the drive.file scope and it can't be combined
    with any other. So we must NOT send ``include_granted_scopes`` — that would merge
    any previously granted scope (e.g. full ``drive``) and trigger ``invalid_scope``.
    """
    return {
        "prompt": "consent",
        "trigger_onepick": "true",
        "allow_folder_selection": "true" if allow_folders else "false",
        "access_type": "offline",
    }


def extract_pick_result(query: dict) -> tuple[str, list[str]]:
    """Pull (auth_code, picked_file_ids) out of the redirect query.

    Accepts values as plain strings or as 1-element lists (``parse_qs`` style).
    """
    def val(key: str) -> str:
        v = query.get(key, "")
        if isinstance(v, (list, tuple)):
            v = v[0] if v else ""
        return v or ""

    if val("error"):
        raise PickerError(val("error"))
    code = val("code")
    if not code:
        raise PickerError("No authorization code in Picker redirect")
    raw_ids = val("picked_file_ids")
    file_ids = [fid for fid in raw_ids.split(",") if fid]
    return code, file_ids


_SUCCESS_HTML = (
    "<!doctype html><html><body style='font-family:sans-serif;background:#0E1116;"
    "color:#E6EDF3;text-align:center;padding-top:80px'>"
    "<h2>⚡ ElectriDrive</h2><p>Selection received. You can close this tab and "
    "return to the app.</p></body></html>"
)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.server.captured = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(_SUCCESS_HTML.encode("utf-8"))

    def log_message(self, *args):  # silence the default stderr logging
        pass


def pick_from_drive(allow_folders: bool = True, timeout: float = 300.0) -> PickResult:
    """Run the desktop Picker and return granted credentials + picked file ids."""
    try:
        from google_auth_oauthlib.flow import Flow
    except Exception as exc:  # pragma: no cover
        raise PickerError("Google OAuth dependencies are not installed.") from exc

    from electridrive.google_api import app_client

    if not app_client.is_configured():
        raise PickerError("No OAuth client configured for the Picker.")

    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    httpd.captured = None
    httpd.timeout = 1.0
    port = httpd.server_address[1]

    flow = Flow.from_client_config(
        app_client.client_config(), scopes=[DEFAULT_SCOPE], autogenerate_code_verifier=True
    )
    flow.redirect_uri = f"http://localhost:{port}/"
    auth_url, state = flow.authorization_url(**picker_auth_params(allow_folders))

    LOGGER.info("Opening Drive Picker in browser")
    webbrowser.open(auth_url)

    deadline = time.time() + timeout
    try:
        while httpd.captured is None and time.time() < deadline:
            httpd.handle_request()
    finally:
        httpd.server_close()

    if httpd.captured is None:
        raise PickerError("Timed out waiting for Drive Picker selection")
    if httpd.captured.get("state") and httpd.captured["state"] != state:
        raise PickerError("State mismatch in Picker redirect")

    code, file_ids = extract_pick_result(httpd.captured)
    flow.fetch_token(code=code)
    save_credentials(flow.credentials)
    LOGGER.info("Picker granted %d item(s)", len(file_ids))
    return PickResult(flow.credentials, file_ids)
