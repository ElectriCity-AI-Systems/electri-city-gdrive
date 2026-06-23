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
    dest_dir = Path(dest_dir)
    items: list[DownloadItem] = []

    if not remote.is_folder:
        path, is_doc, export_mime = _dest_for(remote, dest_dir)
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
    _walk_folder(client, remote.id, folder_dir, items)
    return items


def _walk_folder(client, folder_id: str, folder_dir: Path, items: list[DownloadItem]) -> None:
    page_token: str | None = None
    while True:
        listing = client.list_folder(folder_id, page_token)
        for child in listing.files:
            if child.is_folder:
                _walk_folder(client, child.id, folder_dir / sanitize_name(child.name), items)
            else:
                path, is_doc, export_mime = _dest_for(child, folder_dir)
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
