#!/usr/bin/env python3
"""Write the checked channel and source provenance required by the PyInstaller spec."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = ROOT / "electridrive"
sys.path.insert(0, str(ROOT))

from electridrive.build_gate import (  # noqa: E402
    CUSTOMER_CHANNEL,
    QA_CHANNEL,
    QA_MARKER_CONTENT,
    QA_MARKER_NAME,
)

_REVISION = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_REPOSITORY = "ElectriCity-AI-Systems/electri-city-gdrive"


def main(argv: list[str] | None = None) -> int:
    channel = os.environ.get("ELECTRIDRIVE_BUILD_CHANNEL", CUSTOMER_CHANNEL)
    if channel not in {CUSTOMER_CHANNEL, QA_CHANNEL}:
        print("Unsupported build channel.", file=sys.stderr)
        return 1
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        print("Build provenance requires a Git source checkout.", file=sys.stderr)
        return 1
    if not _REVISION.fullmatch(revision) or dirty:
        print("Customer and QA builds require a clean, committed source checkout.", file=sys.stderr)
        return 1

    action_revision = os.environ.get("GITHUB_SHA")
    action_repository = os.environ.get("GITHUB_REPOSITORY")
    if action_revision and action_revision != revision:
        print("Build provenance does not match the checked-out workflow revision.", file=sys.stderr)
        return 1
    if action_repository and action_repository != _REPOSITORY:
        print("Build provenance is restricted to the official repository.", file=sys.stderr)
        return 1

    channel_file = PACKAGE_DIR / "build_channel.txt"
    provenance_file = PACKAGE_DIR / "build_provenance.json"
    marker_file = PACKAGE_DIR / QA_MARKER_NAME
    baked_file = PACKAGE_DIR / "google_api" / "client_baked.json"
    if any(path.exists() or path.is_symlink() for path in (channel_file, provenance_file, marker_file)):
        print("Refusing to overwrite pre-existing build metadata.", file=sys.stderr)
        return 1
    if channel == QA_CHANNEL and (baked_file.exists() or baked_file.is_symlink()):
        print("CI-QA builds cannot include publisher OAuth configuration.", file=sys.stderr)
        return 1
    if channel == CUSTOMER_CHANNEL and (marker_file.exists() or marker_file.is_symlink()):
        print("Customer candidates cannot include a CI-QA marker.", file=sys.stderr)
        return 1
    try:
        with channel_file.open("x", encoding="utf-8") as stream:
            stream.write(channel + "\n")
        provenance = {
            "repository": _REPOSITORY,
            "source_revision": revision,
            "source_tree_clean": True,
            "channel": channel,
        }
        with provenance_file.open("x", encoding="utf-8") as stream:
            json.dump(provenance, stream, sort_keys=True)
            stream.write("\n")
        if channel == QA_CHANNEL:
            with marker_file.open("x", encoding="utf-8") as stream:
                stream.write(QA_MARKER_CONTENT)
    except (OSError, ValueError):
        channel_file.unlink(missing_ok=True)
        provenance_file.unlink(missing_ok=True)
        marker_file.unlink(missing_ok=True)
        print("Build metadata could not be created for the selected channel.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
