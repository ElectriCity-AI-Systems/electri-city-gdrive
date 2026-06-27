from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

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
    "-journal",
    "-shm",
    "-wal",
}


@dataclass(frozen=True)
class SyncRules:
    include_hidden: bool = False
    excluded_names: set[str] = field(default_factory=lambda: set(DEFAULT_EXCLUDED_NAMES))
    excluded_suffixes: set[str] = field(default_factory=lambda: set(DEFAULT_EXCLUDED_SUFFIXES))
    max_file_size_bytes: int | None = None

    def is_excluded(self, path: Path, root: Path | None = None) -> bool:
        parts = path.parts
        if root is not None:
            try:
                parts = path.relative_to(root).parts
            except ValueError:
                parts = path.parts

        for part in parts:
            if part in self.excluded_names:
                return True
            if not self.include_hidden and part.startswith("."):
                return True

        name = path.name
        if any(name.endswith(suffix) for suffix in self.excluded_suffixes):
            return True

        if path.is_file() and self.max_file_size_bytes is not None:
            try:
                if path.stat().st_size > self.max_file_size_bytes:
                    return True
            except OSError:
                return True
        return False


def assert_no_delete_allowed() -> None:
    raise PermissionError("ElectriDrive v1.0 blocks all delete operations by design.")
