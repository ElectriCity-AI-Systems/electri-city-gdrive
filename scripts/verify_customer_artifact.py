#!/usr/bin/env python3
"""Verify the embedded build channel and customer OAuth config in an artifact."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from electridrive.build_gate import (  # noqa: E402
    CUSTOMER_CHANNEL,
    QA_CHANNEL,
    QA_MARKER_NAME,
    validate_build_inputs,
)
from electridrive.google_api.release_config import validate_customer_config

_GLIBC = re.compile(r"\(GLIBC_(\d+(?:\.\d+)+)\)")


def _glibc_errors(root: Path, maximum: str) -> list[str]:
    candidates = []
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            with path.open("rb") as stream:
                if stream.read(4) == b"\x7fELF":
                    candidates.append(path)
        except OSError:
            continue
    if not candidates:
        return []
    if not shutil.which("objdump"):
        return ["objdump is required for the artifact GLIBC compatibility check"]
    limit = tuple(int(part) for part in maximum.split("."))
    errors = []
    for path in candidates:
        result = subprocess.run(
            ["objdump", "-T", str(path)], capture_output=True, text=True, check=False
        )
        if result.returncode:
            errors.append("cannot inspect an ELF file for GLIBC compatibility")
            continue
        versions = [
            tuple(int(part) for part in match.group(1).split("."))
            for line in result.stdout.splitlines()
            if "*UND*" in line
            for match in _GLIBC.finditer(line)
        ]
        if versions and max(versions) > limit:
            errors.append(f"artifact contains an ELF dependency above GLIBC {maximum}")
    return errors


def _appimage_runtime_errors(path: Path, maximum: str) -> list[str]:
    try:
        with path.open("rb") as stream:
            if stream.read(4) != b"\x7fELF":
                return ["AppImage is not an ELF executable"]
    except OSError:
        return ["AppImage runtime could not be inspected"]
    if not shutil.which("objdump"):
        return ["objdump is required for the AppImage runtime compatibility check"]
    result = subprocess.run(
        ["objdump", "-T", str(path)], capture_output=True, text=True, check=False
    )
    if result.returncode:
        return ["AppImage runtime could not be inspected for GLIBC compatibility"]
    limit = tuple(int(part) for part in maximum.split("."))
    versions = [
        tuple(int(part) for part in match.group(1).split("."))
        for line in result.stdout.splitlines()
        if "*UND*" in line
        for match in _GLIBC.finditer(line)
    ]
    if versions and max(versions) > limit:
        return [f"AppImage runtime requires GLIBC above {maximum}"]
    return []


def _extract(path: Path, destination: Path) -> Path:
    if path.is_dir():
        return path
    if path.suffix == ".deb":
        subprocess.run(
            ["dpkg-deb", "--extract", str(path), str(destination)],
            check=True,
            capture_output=True,
            text=True,
        )
        return destination
    if path.suffix == ".AppImage":
        subprocess.run(
            [str(path), "--appimage-extract"],
            cwd=destination,
            check=True,
            capture_output=True,
            text=True,
        )
        return destination / "squashfs-root"
    raise ValueError("artifact must be a .deb, AppImage, or extracted package directory")


def verify(
    path: Path,
    expected_channel: str,
    max_glibc: str = "2.35",
    expected_revision: str | None = None,
) -> list[str]:
    if not path.exists():
        return [f"artifact does not exist: {path}"]
    errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="electridrive-artifact-") as temp:
        try:
            root = _extract(path.resolve(), Path(temp))
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            return [f"cannot inspect artifact: {type(exc).__name__}"]
        if path.suffix == ".AppImage":
            errors.extend(_appimage_runtime_errors(path.resolve(), max_glibc))
        channels = list(root.rglob("build_channel.txt"))
        if len(channels) != 1:
            errors.append("artifact must contain exactly one build-channel watermark")
        else:
            errors.extend(
                validate_build_inputs(
                    channels[0].parent,
                    expected_channel=expected_channel,
                    expected_revision=expected_revision,
                )
            )
        markers = list(root.rglob(QA_MARKER_NAME))
        if expected_channel == QA_CHANNEL and len(markers) != 1:
            errors.append("CI-QA artifact must contain exactly one NOT FOR DISTRIBUTION marker")
        elif expected_channel == CUSTOMER_CHANNEL and markers:
            errors.append("customer candidate must not contain a CI-QA marker")
        errors.extend(_glibc_errors(root, max_glibc))

        clients = list(root.rglob("client_baked.json"))
        if expected_channel == "CUSTOMER-CANDIDATE":
            if not clients:
                errors.append("customer candidate has no embedded OAuth client configuration")
            elif len(clients) != 1:
                errors.append("customer candidate has multiple embedded OAuth configurations")
            else:
                try:
                    data = json.loads(clients[0].read_text(encoding="utf-8"))
                    validate_customer_config(data)
                except (OSError, ValueError):
                    errors.append("embedded customer OAuth and Picker configuration is invalid")
        elif clients:
            errors.append("CI-QA artifact must not include publisher OAuth configuration")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--expected-channel", choices=("CI-QA", "CUSTOMER-CANDIDATE"), required=True)
    parser.add_argument("--max-glibc", default="2.35")
    parser.add_argument("--expected-revision")
    args = parser.parse_args(argv)
    errors = verify(
        args.artifact,
        args.expected_channel,
        args.max_glibc,
        args.expected_revision,
    )
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"OK: verified {args.expected_channel} artifact and embedded configuration")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
