from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from electridrive.google_api.client import RemoteFile, export_format_for

_ILLEGAL = re.compile(r'[/\x00]')


def sanitize_name(name: str) -> str:
    """Make a Drive name safe to use as a single local path component."""
    cleaned = _ILLEGAL.sub("_", name).strip().rstrip(".")
    return cleaned or "untitled"


@dataclass(frozen=True)
class DownloadItem:
    """A single resolved download: which remote file goes to which local path."""

    file_id: str
    dest_path: Path
    name: str
    size: int
    is_google_doc: bool
    export_mime: str | None = None  # set for Google Workspace docs


def _dest_for(remote: RemoteFile, dest_dir: Path) -> tuple[Path, bool, str | None]:
    safe = sanitize_name(remote.name)
    if remote.is_google_doc:
        export_mime, ext = export_format_for(remote.mime_type)
        if not safe.lower().endswith(ext):
            safe = f"{safe}{ext}"
        return dest_dir / safe, True, export_mime
    return dest_dir / safe, False, None


def plan_download(client, remote: RemoteFile, dest_dir: Path) -> list[DownloadItem]:
    """Resolve a remote file/folder into a flat list of concrete download items.

    Folders are walked recursively; Google Workspace docs are marked for export.
    `client` only needs `.list_folder(parent_id, page_token)` -> FileListing.
    """
    dest_dir = Path(dest_dir).expanduser()
    dest_dir.mkdir(parents=True, exist_ok=True)
    if dest_dir.is_symlink():
        raise ValueError(f"Download destination must not be a symlink: {dest_dir}")
    dest_dir = dest_dir.resolve()
    items: list[DownloadItem] = []

    if not remote.is_folder:
        path, is_doc, export_mime = _dest_for(remote, dest_dir)
        _reject_symlink_path(dest_dir, path)
        items.append(
            DownloadItem(
                file_id=remote.id,
                dest_path=path,
                name=remote.name,
                size=remote.size or 0,
                is_google_doc=is_doc,
                export_mime=export_mime,
            )
        )
        return items

    # Folder: create a subdirectory named after it and recurse.
    folder_dir = dest_dir / sanitize_name(remote.name)
    _reject_symlink_path(dest_dir, folder_dir)
    folder_dir.mkdir(parents=True, exist_ok=True)
    _walk_folder(client, remote.id, folder_dir, items, {remote.id})
    return items


def _reject_symlink_path(root: Path, candidate: Path) -> None:
    current = root
    for part in candidate.relative_to(root).parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f"Refusing to traverse symlink in download tree: {candidate}")


def _walk_folder(
    client,
    folder_id: str,
    folder_dir: Path,
    items: list[DownloadItem],
    visited: set[str],
) -> None:
    page_token: str | None = None
    names: set[str] = set()
    while True:
        listing = client.list_folder(folder_id, page_token)
        for child in listing.files:
            safe_name = sanitize_name(child.name)
            if child.is_google_doc:
                _mime, extension = export_format_for(child.mime_type)
                if not safe_name.lower().endswith(extension):
                    safe_name = f"{safe_name}{extension}"
            if safe_name in names:
                raise ValueError(
                    f"Remote names collide at {folder_dir}: {safe_name!r}"
                )
            names.add(safe_name)
            if child.is_folder:
                if child.id in visited:
                    raise ValueError(f"Remote folder cycle detected at {child.name!r}")
                child_dir = folder_dir / safe_name
                _reject_symlink_path(folder_dir, child_dir)
                child_dir.mkdir(parents=True, exist_ok=True)
                visited.add(child.id)
                _walk_folder(client, child.id, child_dir, items, visited)
            else:
                path, is_doc, export_mime = _dest_for(child, folder_dir)
                _reject_symlink_path(folder_dir, path)
                items.append(
                    DownloadItem(
                        file_id=child.id,
                        dest_path=path,
                        name=child.name,
                        size=child.size or 0,
                        is_google_doc=is_doc,
                        export_mime=export_mime,
                    )
                )
        page_token = listing.next_page_token
        if not page_token:
            break
