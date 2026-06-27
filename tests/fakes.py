"""Shared in-memory fake Google Drive for tests (no network, no google libs)."""
from __future__ import annotations

import hashlib
import itertools
from pathlib import Path

from electridrive.google_api.client import (
    DRIVE_FOLDER_MIME,
    FileListing,
    RemoteChange,
    RemoteFile,
    StorageQuota,
)


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


class FakeDrive:
    """Models a small Drive tree. Satisfies the parts of DriveClientProtocol used in tests."""

    def __init__(self):
        self._ids = itertools.count(1)
        self.nodes: dict[str, dict] = {
            "root": {"id": "root", "name": "My Drive", "mime": DRIVE_FOLDER_MIME,
                     "parent": None, "content": None, "modified": "2024-01-01T00:00:00Z",
                     "trashed": False, "starred": False}
        }
        self.uploads: list[tuple] = []
        # Faithful change feed: every mutation appends (seq, RemoteChange). A page
        # token is just the high-water seq; list_changes returns changes with seq >
        # token, mirroring Drive's changes.list contract closely enough to exercise
        # the incremental-sync path.
        self._change_seq = 0
        self._change_log: list[tuple[int, RemoteChange]] = []
        # Call counters so tests can assert the cache path avoids network round-trips.
        self.list_folder_calls = 0
        self.find_folder_calls = 0
        self.list_changes_calls = 0

    # --------------------------------------------------------------- test setup
    def _nid(self, prefix: str) -> str:
        return f"{prefix}-{next(self._ids)}"

    def _record_change(self, node_id: str, removed: bool = False) -> None:
        self._change_seq += 1
        if removed:
            change = RemoteChange(file_id=node_id, removed=True, file=None)
        else:
            change = RemoteChange(file_id=node_id, removed=False,
                                  file=self._to_remote(self.nodes[node_id]))
        self._change_log.append((self._change_seq, change))

    def add_folder(self, name: str, parent: str = "root") -> str:
        nid = self._nid("folder")
        self.nodes[nid] = {"id": nid, "name": name, "mime": DRIVE_FOLDER_MIME,
                           "parent": parent, "content": None, "modified": "2024-01-01T00:00:00Z",
                           "trashed": False, "starred": False}
        self._record_change(nid)
        return nid

    def add_file(self, name: str, content: bytes = b"data", parent: str = "root",
                 mime: str = "application/octet-stream",
                 modified: str = "2024-01-01T00:00:00Z", starred: bool = False) -> str:
        nid = self._nid("file")
        self.nodes[nid] = {"id": nid, "name": name, "mime": mime, "parent": parent,
                           "content": content, "modified": modified,
                           "trashed": False, "starred": starred}
        self._record_change(nid)
        return nid

    def _to_remote(self, node: dict) -> RemoteFile:
        content = node["content"]
        return RemoteFile(
            id=node["id"], name=node["name"], mime_type=node["mime"],
            size=(len(content) if content is not None else None),
            modified_time=node["modified"],
            parents=tuple([node["parent"]] if node["parent"] else []),
            trashed=node["trashed"], starred=node["starred"],
            md5_checksum=(_md5(content) if content is not None else None),
        )

    # ----------------------------------------------------------------- protocol
    def find_folder(self, name: str, parent_id: str | None = None) -> str | None:
        self.find_folder_calls += 1
        for n in self.nodes.values():
            if n["mime"] == DRIVE_FOLDER_MIME and n["name"] == name and not n["trashed"]:
                if parent_id is None or n["parent"] == parent_id:
                    return n["id"]
        return None

    def create_folder(self, name: str, parent_id: str | None = None) -> str:
        return self.add_folder(name, parent_id or "root")

    def ensure_folder_path(self, remote_path: str) -> str:
        parts = [p for p in remote_path.replace("\\", "/").split("/") if p.strip()]
        parent = "root"
        for part in parts:
            found = self.find_folder(part, parent)
            parent = found or self.add_folder(part, parent)
        return parent

    def upload_file(self, local_file: Path, parent_id: str, remote_name: str, progress_cb=None) -> str:
        data = Path(local_file).read_bytes()
        nid = self.add_file(remote_name, content=data, parent=parent_id)
        self.uploads.append((str(local_file), parent_id, remote_name, nid))
        if progress_cb:
            progress_cb(len(data), len(data))
        return nid

    def download_file(self, file_id: str, dest_path: Path, progress_cb=None) -> Path:
        node = self.nodes[file_id]
        data = node["content"] or b""
        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(data)
        if progress_cb:
            progress_cb(len(data), len(data))
        return dest_path

    def export_file(self, file_id: str, dest_path: Path, mime_type=None, progress_cb=None) -> Path:
        node = self.nodes[file_id]
        data = b"EXPORTED:" + (node["content"] or b"")
        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(data)
        if progress_cb:
            progress_cb(len(data), len(data))
        return dest_path

    def list_folder(self, parent_id: str = "root", page_token: str | None = None) -> FileListing:
        self.list_folder_calls += 1
        children = [self._to_remote(n) for n in self.nodes.values()
                    if n["parent"] == parent_id and not n["trashed"]]
        children.sort(key=lambda r: (not r.is_folder, r.name.lower()))
        return FileListing(files=children, next_page_token=None)

    def list_files(self, limit: int = 20) -> list[RemoteFile]:
        files = [self._to_remote(n) for n in self.nodes.values()
                 if n["mime"] != DRIVE_FOLDER_MIME and not n["trashed"]]
        return files[:limit]

    def get_metadata(self, file_id: str) -> RemoteFile:
        return self._to_remote(self.nodes[file_id])

    def get_about(self) -> StorageQuota:
        usage = sum(len(n["content"]) for n in self.nodes.values() if n["content"])
        return StorageQuota(limit=15 * 1024**3, usage=usage, usage_in_drive=usage,
                            user_email="tester@example.com", user_name="Tester")

    def get_start_page_token(self) -> str:
        return str(self._change_seq)

    def list_changes(self, page_token: str):
        self.list_changes_calls += 1
        try:
            since = int(page_token)
        except (TypeError, ValueError):
            since = 0
        changes = [c for (seq, c) in self._change_log if seq > since]
        return changes, None, str(self._change_seq)

    def trash(self, file_id: str) -> None:
        self.nodes[file_id]["trashed"] = True
        self._record_change(file_id)
