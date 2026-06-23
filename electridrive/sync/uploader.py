from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from electridrive.sync.rules import SyncRules


@dataclass(frozen=True)
class UploadItem:
    """A single resolved upload: which local file goes into which remote parent."""

    local_path: Path
    parent_id: str
    name: str
    size: int


def _find_or_create_child(client, name: str, parent_id: str) -> str:
    """Reuse an existing remote child folder if present, else create it."""
    finder = getattr(client, "find_folder", None)
    if finder is not None:
        existing = finder(name, parent_id)
        if existing:
            return existing
    return client.create_folder(name, parent_id)


def plan_upload(client, local_path: Path, parent_id: str, rules: SyncRules | None = None) -> list[UploadItem]:
    """Resolve a local file/folder into concrete upload items under `parent_id`.

    Folders are mirrored on Drive (subfolders created as needed). Exclusion rules
    are honored. `client` needs `.find_folder(name, parent)` (optional) and
    `.create_folder(name, parent)`.
    """
    local_path = Path(local_path).expanduser().resolve()
    rules = rules or SyncRules()
    items: list[UploadItem] = []

    if local_path.is_file():
        items.append(UploadItem(local_path, parent_id, local_path.name, local_path.stat().st_size))
        return items

    if local_path.is_dir():
        root_remote = _find_or_create_child(client, local_path.name, parent_id)
        _walk(client, local_path, local_path, root_remote, rules, items)
    return items


def _walk(client, root: Path, current: Path, remote_parent: str,
          rules: SyncRules, items: list[UploadItem]) -> None:
    for entry in sorted(current.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        if rules.is_excluded(entry, root):
            continue
        if entry.is_dir():
            child_remote = _find_or_create_child(client, entry.name, remote_parent)
            _walk(client, root, entry, child_remote, rules, items)
        elif entry.is_file():
            items.append(UploadItem(entry, remote_parent, entry.name, entry.stat().st_size))
