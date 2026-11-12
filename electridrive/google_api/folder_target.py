"""Exact existing Google Drive folder references, without path creation."""
from __future__ import annotations

import re


def direct_folder_id(remote_folder: str) -> str | None:
    """Recognize only the explicit id:<folder-ID> form; QA roots stay separate."""
    if not isinstance(remote_folder, str):
        raise ValueError("Drive folder target must be text")
    if not remote_folder.startswith("id:"):
        return None
    value = remote_folder[3:]
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,256}", value, re.ASCII):
        raise ValueError("Invalid Drive folder ID target: expected id:<folder-ID>")
    return value


def require_complete_folder_access(remote_folder: str) -> None:
    """Refuse auto-sync with a partially visible drive.file child inventory.

    Picker permission to a folder does not grant access to all existing child
    objects. Automatic synchronization would infer incorrect remote state.
    """
    if direct_folder_id(remote_folder) is None:
        return
    from electridrive.config import FULL_DRIVE_SCOPE, selected_scopes
    if FULL_DRIVE_SCOPE not in selected_scopes():
        raise PermissionError(
            "Existing folder ID synchronization requires full Drive scope; "
            "drive.file/Picker can hide existing child files. "
            "No synchronization started."
        )
