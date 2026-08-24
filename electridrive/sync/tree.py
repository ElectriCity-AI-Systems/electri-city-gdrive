"""Safe, deterministic local-tree inventory helpers for selected sync roots."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from electridrive.sync.rules import SyncRules


@dataclass(frozen=True)
class LocalTree:
    root: Path
    files: tuple[Path, ...]
    directories: tuple[Path, ...]
    excluded: tuple[str, ...]
    errors: tuple[str, ...]

    @property
    def relative_files(self) -> set[str]:
        return {path.relative_to(self.root).as_posix() for path in self.files}

    @property
    def relative_directories(self) -> set[str]:
        return {path.relative_to(self.root).as_posix() for path in self.directories}


def canonical_sync_root(root: Path, *, create: bool = False) -> Path:
    """Resolve the explicitly selected root, optionally creating the root itself."""
    requested = Path(root).expanduser()
    if create:
        requested.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        resolved = requested.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"Selected sync root is unavailable: {requested}: {exc}") from exc
    if not resolved.is_dir():
        raise ValueError(f"Selected sync root is not a directory: {requested}")
    return resolved


def scan_local_tree(root: Path, rules: SyncRules | None = None) -> LocalTree:
    """Inventory included files and folders without following descendant symlinks.

    An excluded directory is counted once and deliberately not traversed. Any
    filesystem error is returned explicitly so a caller cannot report success for
    a tree whose contents were only partially observable.
    """
    rules = rules or SyncRules()
    canonical = canonical_sync_root(root)
    files: list[Path] = []
    directories: list[Path] = []
    excluded: list[str] = []
    errors: list[str] = []

    def walk(current: Path) -> None:
        try:
            with os.scandir(current) as iterator:
                entries = sorted(iterator, key=lambda item: (item.name.casefold(), item.name))
        except OSError as exc:
            rel = current.relative_to(canonical).as_posix() or "."
            errors.append(f"cannot scan {rel}: {type(exc).__name__}")
            return

        for entry in entries:
            path = Path(entry.path)
            rel = path.relative_to(canonical).as_posix()
            try:
                if entry.is_symlink():
                    excluded.append(rel)
                    continue
                is_dir = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
                if not is_dir and not is_file:
                    excluded.append(rel)
                    continue
                size = entry.stat(follow_symlinks=False).st_size if is_file else None
            except OSError as exc:
                errors.append(f"cannot inspect {rel}: {type(exc).__name__}")
                continue

            if rules.is_excluded(
                path,
                canonical,
                is_file=is_file,
                size=size,
            ):
                excluded.append(rel)
                continue
            if is_dir:
                directories.append(path)
                walk(path)
            else:
                files.append(path)

    walk(canonical)
    return LocalTree(
        root=canonical,
        files=tuple(files),
        directories=tuple(directories),
        excluded=tuple(excluded),
        errors=tuple(errors),
    )


def safe_local_path(root: Path, relative_path: str) -> Path:
    """Return a selected-root child path, rejecting traversal and symlink parents."""
    canonical = canonical_sync_root(root)
    relative = PurePosixPath(relative_path)
    if relative.is_absolute() or not relative.parts:
        raise ValueError(f"Unsafe relative sync path: {relative_path!r}")
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError(f"Unsafe relative sync path: {relative_path!r}")

    candidate = canonical
    for part in relative.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ValueError(f"Refusing to traverse symlink in selected root: {relative_path}")
    return candidate
