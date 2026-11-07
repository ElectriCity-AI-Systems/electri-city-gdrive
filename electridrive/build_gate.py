from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from electridrive.google_api.release_config import validate_customer_config

CUSTOMER_CHANNEL = "CUSTOMER-CANDIDATE"
QA_CHANNEL = "CI-QA"
QA_MARKER_NAME = "QA-NOT-FOR-DISTRIBUTION.txt"
QA_MARKER_CONTENT = "ElectriDrive CI-QA artifact — NOT FOR DISTRIBUTION\n"
_REVISION = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_REPOSITORY = "ElectriCity-AI-Systems/electri-city-gdrive"


def validate_build_inputs(
    root: Path,
    *,
    expected_channel: str | None = None,
    expected_revision: str | None = None,
    check_source_checkout: bool = False,
) -> list[str]:
    root = Path(root)
    channel_file = root / "build_channel.txt"
    provenance_file = root / "build_provenance.json"
    qa_marker = root / QA_MARKER_NAME
    baked_client = root / "google_api" / "client_baked.json"
    errors: list[str] = []

    try:
        channel = channel_file.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ["build channel metadata is missing or unreadable"]
    if channel not in {CUSTOMER_CHANNEL, QA_CHANNEL}:
        errors.append("build channel metadata is unsupported")
    if expected_channel and channel != expected_channel:
        errors.append("build channel does not match the requested channel")

    try:
        provenance: Any = json.loads(provenance_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        provenance = None
    if not isinstance(provenance, dict):
        errors.append("build provenance is missing or malformed")
        provenance = {}
    revision = provenance.get("source_revision")
    if (
        provenance.get("repository") != _REPOSITORY
        or provenance.get("channel") != channel
        or provenance.get("source_tree_clean") is not True
        or not isinstance(revision, str)
        or not _REVISION.fullmatch(revision)
    ):
        errors.append("build provenance does not identify a clean repository revision")
    if expected_revision and revision != expected_revision:
        errors.append("build provenance does not match the requested source revision")
    if check_source_checkout:
        try:
            current = subprocess.run(
                ["git", "rev-parse", "--verify", "HEAD"],
                cwd=root.parent,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            dirty = subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=normal"],
                cwd=root.parent,
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        except (OSError, subprocess.CalledProcessError):
            errors.append("build provenance cannot be checked against the source checkout")
        else:
            if current != revision or dirty:
                errors.append("build provenance does not match a clean source checkout")

    if channel == QA_CHANNEL:
        try:
            marker_matches = qa_marker.read_text(encoding="utf-8") == QA_MARKER_CONTENT
        except (OSError, UnicodeError):
            marker_matches = False
        if not marker_matches:
            errors.append("CI-QA build is missing its NOT FOR DISTRIBUTION marker")
        if baked_client.exists() or baked_client.is_symlink():
            errors.append("CI-QA build must not contain publisher OAuth configuration")
    elif channel == CUSTOMER_CHANNEL:
        if qa_marker.exists() or qa_marker.is_symlink():
            errors.append("customer candidate must not contain the CI-QA marker")
        try:
            data = json.loads(baked_client.read_text(encoding="utf-8"))
            validate_customer_config(data)
        except (OSError, UnicodeError, ValueError):
            errors.append("customer candidate requires valid publisher OAuth and Picker configuration")
    return errors
