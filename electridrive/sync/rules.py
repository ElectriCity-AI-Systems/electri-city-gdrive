from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

DEFAULT_EXCLUDED_NAMES = {
    ".git",
    ".cache",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".electridrive-trash",
    "tmp",
    "temp",
    "trash",
    ".trash",
    "lost+found",
}

DEFAULT_EXCLUDED_SUFFIXES = {
    ".tmp",
    ".part",
    ".crdownload",
    ".swp",
    ".bak~",
}


@dataclass(frozen=True)
class SyncRules:
    include_hidden: bool = False
    excluded_names: set[str] = field(default_factory=lambda: set(DEFAULT_EXCLUDED_NAMES))
    excluded_suffixes: set[str] = field(default_factory=lambda: set(DEFAULT_EXCLUDED_SUFFIXES))
    max_file_size_bytes: int | None = None

    def is_excluded(
        self,
        path: Path,
        root: Path | None = None,
        *,
        is_file: bool | None = None,
        size: int | None = None,
    ) -> bool:
        parts = path.parts
        if root is not None:
            try:
                parts = path.relative_to(root).parts
            except ValueError:
                parts = path.parts

        return self.is_excluded_parts(
            parts,
            is_file=path.is_file() if is_file is None else is_file,
            size=size,
            path=path,
        )

    def is_excluded_relative(
        self,
        relative_path: str,
        *,
        is_file: bool,
        size: int | None = None,
    ) -> bool:
        """Apply the same rules to a remote path without touching local disk."""
        return self.is_excluded_parts(
            Path(relative_path).parts,
            is_file=is_file,
            size=size,
        )

    def is_excluded_parts(
        self,
        parts: Iterable[str],
        *,
        is_file: bool,
        size: int | None = None,
        path: Path | None = None,
    ) -> bool:
        parts = tuple(parts)
        for part in parts:
            if part in self.excluded_names:
                return True
            if not self.include_hidden and part.startswith("."):
                return True

        name = parts[-1] if parts else ""
        if any(name.endswith(suffix) for suffix in self.excluded_suffixes):
            return True

        if is_file and self.max_file_size_bytes is not None:
            if size is None and path is not None:
                try:
                    size = path.stat().st_size
                except OSError:
                    return True
            if size is not None and size > self.max_file_size_bytes:
                return True
        return False


def assert_no_delete_allowed() -> None:
    raise PermissionError("ElectriDrive v1.0 blocks all delete operations by design.")
