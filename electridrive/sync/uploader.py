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


def _cached_child_folder(db, name: str, parent_id: str) -> str | None:
    """Resolve an existing child *folder* id from the local remote-node cache (no
    network). Returns None when no db / not cached."""
    if db is None:
        return None
    getter = getattr(db, "get_cached_children", None)
    if getter is None:
        return None
    try:
        for row in getter(parent_id):
            if row["is_folder"] and row["name"] == name and not row["trashed"]:
                return row["id"]
    except Exception:
        return None
    return None


def _find_or_create_child(client, name: str, parent_id: str,
                          db=None, memo: dict | None = None) -> str:
    """Reuse an existing remote child folder if present, else create it.

    Resolution order, cheapest first: per-run memo → local remote-node cache →
    live ``find_folder`` → ``create_folder``."""
    if memo is not None and (parent_id, name) in memo:
        return memo[(parent_id, name)]
    fid = _cached_child_folder(db, name, parent_id)
    if not fid:
        finder = getattr(client, "find_folder", None)
        if finder is not None:
            fid = finder(name, parent_id)
    if not fid:
        fid = client.create_folder(name, parent_id)
    if memo is not None:
        memo[(parent_id, name)] = fid
    return fid


def plan_upload(client, local_path: Path, parent_id: str,
                rules: SyncRules | None = None, db=None) -> list[UploadItem]:
    """Resolve a local file/folder into concrete upload items under `parent_id`.

    Folders are mirrored on Drive (subfolders created as needed). Exclusion rules
    are honored. `client` needs `.find_folder(name, parent)` (optional) and
    `.create_folder(name, parent)`. When `db` (a ``SyncDatabase`` with a fresh
    remote-node cache) is supplied, existing folders are resolved from the cache
    first, avoiding a `find_folder` round-trip per directory level.
    """
    local_path = Path(local_path).expanduser().resolve()
    rules = rules or SyncRules()
    items: list[UploadItem] = []
    memo: dict[tuple[str, str], str] = {}

    if local_path.is_file():
        items.append(UploadItem(local_path, parent_id, local_path.name, local_path.stat().st_size))
        return items

    if local_path.is_dir():
        root_remote = _find_or_create_child(client, local_path.name, parent_id, db, memo)
        _walk(client, local_path, local_path, root_remote, rules, items, db, memo)
    return items


def _walk(client, root: Path, current: Path, remote_parent: str,
          rules: SyncRules, items: list[UploadItem], db=None,
          memo: dict | None = None) -> None:
    for entry in sorted(current.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        if rules.is_excluded(entry, root):
            continue
        if entry.is_dir():
            child_remote = _find_or_create_child(client, entry.name, remote_parent, db, memo)
            _walk(client, root, entry, child_remote, rules, items, db, memo)
        elif entry.is_file():
            items.append(UploadItem(entry, remote_parent, entry.name, entry.stat().st_size))
