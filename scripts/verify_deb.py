#!/usr/bin/env python3
"""Verify that an ElectriDrive Debian package is launchable and portable."""

from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


SEARCH_PATHS = (
    "usr/local/bin",
    "usr/bin",
    "bin",
    "usr/local/sbin",
    "usr/sbin",
    "sbin",
)
GLIBC_REFERENCE = re.compile(r"\(GLIBC_(\d+(?:\.\d+)+)\)")
REQUIRED_QT_FILES = (
    "libQt6Core.so.6",
    "libQt6Gui.so.6",
    "libQt6Widgets.so.6",
    "libqoffscreen.so",
    "libqxcb.so",
)
REQUIRED_DEPENDENCY_GROUPS = (
    ("libfuse3-3", "fuse3"),
    ("libegl1",),
    ("libgl1",),
    ("libwayland-cursor0",),
    ("libwayland-egl1",),
)


class PackageError(RuntimeError):
    """Raised when the package structure cannot be inspected."""


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=True, text=True, capture_output=True)


def _version(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def _package_field(package: Path, field: str) -> str:
    try:
        return _run("dpkg-deb", "--field", str(package), field).stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise PackageError(f"cannot read Debian field {field}: {exc}") from exc


def _desktop_entry(path: Path) -> dict[str, str]:
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    try:
        with path.open(encoding="utf-8") as stream:
            parser.read_file(stream)
    except (OSError, configparser.Error) as exc:
        raise PackageError(f"cannot parse {path.name}: {exc}") from exc
    if "Desktop Entry" not in parser:
        raise PackageError(f"{path.name} has no [Desktop Entry] group")
    return dict(parser["Desktop Entry"])


def _command(value: str, key: str, desktop: Path) -> str:
    try:
        words = shlex.split(value, posix=True)
    except ValueError as exc:
        raise PackageError(f"{desktop.name}: invalid {key}: {exc}") from exc
    if not words:
        raise PackageError(f"{desktop.name}: empty {key}")
    command = words[0]
    if command.startswith("%"):
        raise PackageError(f"{desktop.name}: {key} starts with a field code")
    return command


def _under_root(root: Path, candidate: Path) -> Path:
    candidate = Path(os.path.abspath(candidate))
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise PackageError(f"package path escapes extraction root: {candidate}") from exc
    return candidate


def _follow_packaged_symlink(root: Path, candidate: Path) -> Path:
    """Follow a final-component symlink while treating absolute targets as package paths."""
    current = _under_root(root, candidate)
    for _ in range(40):
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            return current
        if not stat.S_ISLNK(mode):
            return current
        target = os.readlink(current)
        if os.path.isabs(target):
            current = root / target.lstrip("/")
        else:
            current = current.parent / target
        current = _under_root(root, Path(os.path.normpath(current)))
    raise PackageError(f"too many symlinks while resolving {candidate}")


def _launcher_target(root: Path, command: str) -> tuple[Path | None, list[Path]]:
    if os.path.isabs(command):
        candidates = [root / command.lstrip("/")]
    elif "/" in command:
        raise PackageError(f"launcher command must be absolute or a PATH name: {command}")
    else:
        candidates = [root / directory / command for directory in SEARCH_PATHS]

    for candidate in candidates:
        resolved = _follow_packaged_symlink(root, candidate)
        if resolved.exists():
            return resolved, candidates
    return None, candidates


def _verify_launcher(root: Path, desktop: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    notes: list[str] = []
    try:
        entry = _desktop_entry(desktop)
    except PackageError as exc:
        return [str(exc)], notes

    for key in ("Exec", "TryExec"):
        value = entry.get(key)
        if not value:
            if key == "Exec":
                errors.append(f"{desktop.name}: missing Exec")
            continue
        try:
            command = _command(value, key, desktop)
            target, candidates = _launcher_target(root, command)
        except PackageError as exc:
            errors.append(str(exc))
            continue
        if target is None:
            locations = ", ".join("/" + str(p.relative_to(root)) for p in candidates)
            errors.append(
                f"{desktop.name}: {key} command {command!r} is absent from package "
                f"(checked {locations})"
            )
            continue
        mode = target.stat().st_mode
        if not stat.S_ISREG(mode):
            errors.append(f"{desktop.name}: {key} target /{target.relative_to(root)} is not a file")
        elif not mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
            errors.append(
                f"{desktop.name}: {key} target /{target.relative_to(root)} is not executable"
            )
        else:
            notes.append(
                f"{desktop.name}: {key}={command} -> /{target.relative_to(root)} (executable)"
            )

    validator = shutil.which("desktop-file-validate")
    if validator:
        result = subprocess.run(
            [validator, str(desktop)], text=True, capture_output=True, check=False
        )
        if result.returncode:
            detail = (result.stdout + result.stderr).strip()
            errors.append(f"{desktop.name}: desktop-file-validate failed: {detail}")
    return errors, notes


def _elf_files(root: Path):
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            with path.open("rb") as stream:
                if stream.read(4) == b"\x7fELF":
                    yield path
        except OSError:
            continue


def _glibc_requirements(root: Path) -> tuple[dict[Path, str], list[str]]:
    requirements: dict[Path, str] = {}
    errors: list[str] = []
    if not shutil.which("objdump"):
        return requirements, ["objdump is required for the GLIBC compatibility check"]

    for path in _elf_files(root):
        result = subprocess.run(
            ["objdump", "-T", str(path)], text=True, capture_output=True, check=False
        )
        if result.returncode:
            errors.append(f"objdump failed for /{path.relative_to(root)}")
            continue
        versions = [
            match.group(1)
            for line in result.stdout.splitlines()
            if "*UND*" in line
            for match in GLIBC_REFERENCE.finditer(line)
        ]
        if versions:
            requirements[path] = max(versions, key=_version)
    return requirements, errors


def _verify_qt(root: Path) -> tuple[list[str], list[str]]:
    names = {path.name for path in root.rglob("*") if path.is_file()}
    missing = [name for name in REQUIRED_QT_FILES if name not in names]
    if missing:
        return ["missing bundled Qt runtime files: " + ", ".join(missing)], []
    return [], ["bundled Qt Core/Gui/Widgets plus offscreen and XCB plugins are present"]


def _verify_baked_client(root: Path) -> tuple[list[str], list[str]]:
    clients = list(root.rglob("client_baked.json"))
    if not clients:
        return ["release package has no baked OAuth client configuration"], []
    try:
        data = json.loads(clients[0].read_text(encoding="utf-8"))
        section = data.get("installed") or data.get("web") or data
        client_id = section.get("client_id", "")
    except (OSError, ValueError, AttributeError) as exc:
        return [f"baked OAuth client configuration is invalid: {exc}"], []
    if not client_id or "REPLACE_WITH_YOUR_" in str(client_id):
        return ["release package has only a placeholder OAuth client"], []
    return [], ["baked OAuth client configuration is present (values not displayed)"]


def verify(
    package: Path,
    expected_version: str | None,
    max_glibc: str,
    require_baked_client: bool,
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    notes: list[str] = []
    if not package.is_file():
        return [f"package does not exist: {package}"], notes

    try:
        name = _package_field(package, "Package")
        version = _package_field(package, "Version")
        architecture = _package_field(package, "Architecture")
        depends = _package_field(package, "Depends")
    except PackageError as exc:
        return [str(exc)], notes

    if name != "electridrive":
        errors.append(f"unexpected Package field: {name!r}")
    if expected_version and version != expected_version:
        errors.append(f"package version {version!r} does not equal {expected_version!r}")
    if architecture != "amd64":
        errors.append(f"unexpected Architecture field: {architecture!r}")
    dependency_names = set(
        re.findall(r"(?:^|[,|])\s*([A-Za-z0-9][A-Za-z0-9+.-]*)", depends)
    )
    for alternatives in REQUIRED_DEPENDENCY_GROUPS:
        if not dependency_names.intersection(alternatives):
            errors.append(
                "missing Debian runtime dependency: " + " | ".join(alternatives)
            )
    notes.append(f"metadata: {name} {version} {architecture}")
    notes.append(f"declared runtime dependencies: {depends}")

    with tempfile.TemporaryDirectory(prefix="electridrive-deb-") as temp_dir:
        root = Path(temp_dir).resolve()
        try:
            _run("dpkg-deb", "--extract", str(package), str(root))
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            errors.append(f"cannot extract Debian package: {exc}")
            return errors, notes

        desktops = sorted((root / "usr/share/applications").glob("*.desktop"))
        if not desktops:
            errors.append("package contains no desktop entry")
        for desktop in desktops:
            launcher_errors, launcher_notes = _verify_launcher(root, desktop)
            errors.extend(launcher_errors)
            notes.extend(launcher_notes)

        qt_errors, qt_notes = _verify_qt(root)
        errors.extend(qt_errors)
        notes.extend(qt_notes)

        requirements, glibc_errors = _glibc_requirements(root)
        errors.extend(glibc_errors)
        if requirements:
            highest_path, highest = max(
                requirements.items(), key=lambda item: _version(item[1])
            )
            notes.append(
                f"highest GLIBC requirement: {highest} "
                f"(/{highest_path.relative_to(root)})"
            )
            incompatible = sorted(
                (
                    (path, required)
                    for path, required in requirements.items()
                    if _version(required) > _version(max_glibc)
                ),
                key=lambda item: (_version(item[1]), str(item[0])),
                reverse=True,
            )
            for path, required in incompatible:
                errors.append(
                    f"/{path.relative_to(root)} requires GLIBC_{required}, "
                    f"above supported baseline {max_glibc}"
                )

        if require_baked_client:
            client_errors, client_notes = _verify_baked_client(root)
            errors.extend(client_errors)
            notes.extend(client_notes)

    return errors, notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    parser.add_argument("--expected-version")
    parser.add_argument(
        "--max-glibc",
        default="2.35",
        help="maximum permitted referenced GLIBC symbol version (default: 2.35)",
    )
    parser.add_argument(
        "--require-baked-client",
        action="store_true",
        help="require a non-placeholder built-in OAuth client without printing its values",
    )
    args = parser.parse_args(argv)

    errors, notes = verify(
        args.package.resolve(),
        args.expected_version,
        args.max_glibc,
        args.require_baked_client,
    )
    for note in notes:
        print(f"OK: {note}")
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"OK: verified {args.package}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
