from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from electridrive.sync.rules import SyncRules
from electridrive.sync.tree import scan_local_tree


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
    local_path = Path(local_path).expanduser()
    rules = rules or SyncRules()
    items: list[UploadItem] = []

    if local_path.is_symlink():
        return items

    local_path = local_path.resolve()
    if local_path.is_file():
        if rules.is_excluded(
            local_path, local_path.parent, is_file=True, size=local_path.stat().st_size
        ):
            return items
        items.append(UploadItem(local_path, parent_id, local_path.name, local_path.stat().st_size))
        return items

    if local_path.is_dir():
        root_remote = _find_or_create_child(client, local_path.name, parent_id)
        tree = scan_local_tree(local_path, rules)
        if tree.errors:
            raise RuntimeError("Incomplete local traversal: " + "; ".join(tree.errors))

        remote_dirs: dict[str, str] = {"": root_remote}
        for directory in sorted(
            tree.directories,
            key=lambda path: (len(path.relative_to(local_path).parts), path.as_posix()),
        ):
            relative = directory.relative_to(local_path)
            parent_rel = relative.parent.as_posix()
            parent_rel = "" if parent_rel == "." else parent_rel
            remote_dirs[relative.as_posix()] = _find_or_create_child(
                client, directory.name, remote_dirs[parent_rel]
            )

        for file_path in tree.files:
            relative = file_path.relative_to(local_path)
            parent_rel = relative.parent.as_posix()
            parent_rel = "" if parent_rel == "." else parent_rel
            items.append(
                UploadItem(
                    file_path,
                    remote_dirs[parent_rel],
                    file_path.name,
                    file_path.stat().st_size,
                )
            )
    return items
