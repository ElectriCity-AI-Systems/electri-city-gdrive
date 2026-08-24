from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from electridrive.google_api.client import DriveClientProtocol
from electridrive.storage.database import SyncDatabase
from electridrive.sync.hasher import sha256_file
from electridrive.sync.rules import SyncRules
from electridrive.sync.tree import canonical_sync_root, safe_local_path, scan_local_tree

LOGGER = logging.getLogger(__name__)
ProgressCallback = Callable[[str, int, int], None]
LogCallback = Callable[[str], None]


@dataclass
class SyncResult:
    scanned: int = 0
    scanned_folders: int = 0
    uploaded: int = 0
    folders_synced: int = 0
    skipped: int = 0
    excluded: int = 0
    failed: int = 0
    unexplained_omissions: int = 0
    errors: list[str] = field(default_factory=list)


class UploadOnlySyncEngine:
    def __init__(
        self,
        *,
        drive_client: DriveClientProtocol,
        database: SyncDatabase,
        rules: SyncRules | None = None,
        log_callback: LogCallback | None = None,
        progress_callback: ProgressCallback | None = None,
    ):
        self.drive_client = drive_client
        self.database = database
        self.rules = rules or SyncRules()
        self.log_callback = log_callback
        self.progress_callback = progress_callback

    def _log(self, message: str) -> None:
        LOGGER.info(message)
        if self.log_callback:
            self.log_callback(message)

    def iter_candidate_files(self, root: Path) -> Iterable[Path]:
        yield from scan_local_tree(root, self.rules).files

    def should_upload(self, file_path: Path, sha256: str) -> bool:
        stat = file_path.stat()
        record = self.database.get_file(str(file_path))
        if record is None:
            return True
        return not (record.size == stat.st_size and record.mtime_ns == stat.st_mtime_ns and record.sha256 == sha256)

    def sync_up(self, local_root: Path, remote_folder: str) -> SyncResult:
        result = SyncResult()
        local_root = canonical_sync_root(local_root)

        self._log(f"Starting upload-only sync: {local_root} -> {remote_folder}")
        root_remote_id = self.drive_client.ensure_folder_path(remote_folder)
        tree = scan_local_tree(local_root, self.rules)
        files = list(tree.files)
        result.scanned_folders = len(tree.directories)
        result.excluded = len(tree.excluded)
        for error in tree.errors:
            result.failed += 1
            result.errors.append(error)
            self._log(f"Local traversal failed: {error}")

        synchronized_folders: set[str] = set()
        remote_directories: dict[str, str] = {"": root_remote_id}
        for directory in sorted(
            tree.directories,
            key=lambda path: (len(path.relative_to(local_root).parts), path.as_posix()),
        ):
            relative = directory.relative_to(local_root).as_posix()
            folder_path = f"{remote_folder.rstrip('/')}/{relative}"
            try:
                parent_relative = directory.parent.relative_to(local_root).as_posix()
                parent_relative = "" if parent_relative == "." else parent_relative
                parent_id = remote_directories[parent_relative]
                finder = getattr(self.drive_client, "find_folder", None)
                creator = getattr(self.drive_client, "create_folder", None)
                existing = (
                    finder(directory.name, parent_id) if callable(finder) else None
                )
                if existing:
                    remote_id = existing
                elif callable(creator):
                    remote_id = creator(directory.name, parent_id)
                else:
                    # Compatibility fallback for minimal client implementations.
                    remote_id = self.drive_client.ensure_folder_path(folder_path)
                remote_directories[relative] = remote_id
                # Use an absolute local key: two selected roots may both contain
                # a directory called "sub" and must not overwrite each other's
                # state entry.
                self.database.upsert_folder(
                    local_rel_path=str(directory),
                    remote_path=folder_path,
                    remote_id=remote_id,
                )
                synchronized_folders.add(relative)
                result.folders_synced += 1
            except Exception as exc:
                result.failed += 1
                error = f"Failed folder {relative}: {exc}"
                result.errors.append(error)
                self._log(error)

        total = len(files)
        synchronized_files: set[str] = set()

        for index, file_path in enumerate(files, start=1):
            result.scanned += 1
            if self.progress_callback:
                self.progress_callback(str(file_path), index, total)
            try:
                relative = file_path.relative_to(local_root)
                file_path = safe_local_path(local_root, relative.as_posix())
                if not file_path.is_file():
                    raise ValueError("selected entry is no longer a regular file")
                digest = sha256_file(file_path)
                parent_relative = relative.parent
                if str(parent_relative) in {".", ""}:
                    parent_id = root_remote_id
                    remote_path = f"{remote_folder}/{relative.name}"
                else:
                    folder_path = f"{remote_folder}/{parent_relative.as_posix()}"
                    parent_id = remote_directories[parent_relative.as_posix()]
                    self.database.upsert_folder(
                        local_rel_path=str(file_path.parent),
                        remote_path=folder_path,
                        remote_id=parent_id,
                    )
                    remote_path = f"{folder_path}/{relative.name}"

                previous = self.database.get_file(str(file_path))
                finder = getattr(self.drive_client, "find_file", None)
                remote_id = finder(file_path.name, parent_id) if callable(finder) else None

                if (
                    previous is not None
                    and not self.should_upload(file_path, digest)
                    and (not callable(finder) or remote_id == previous.remote_id)
                ):
                    result.skipped += 1
                    synchronized_files.add(relative.as_posix())
                    self._log(f"Skipped unchanged: {relative}")
                    continue

                if remote_id is None and previous is not None and not callable(finder):
                    remote_id = previous.remote_id

                if remote_id is None:
                    remote_id = self.drive_client.upload_file(
                        file_path, parent_id, file_path.name
                    )
                    operation = "Uploaded"
                else:
                    # The remote ID is the canonical identity. A changed local
                    # file replaces its media instead of creating a same-named
                    # Drive object, including after local state was lost.
                    self.drive_client.update_file(remote_id, file_path)
                    operation = "Updated"
                stat = file_path.stat()
                self.database.upsert_file(
                    local_path=str(file_path),
                    remote_id=remote_id,
                    remote_path=remote_path,
                    size=stat.st_size,
                    mtime_ns=stat.st_mtime_ns,
                    sha256=digest,
                    status="uploaded",
                )
                result.uploaded += 1
                synchronized_files.add(relative.as_posix())
                self._log(f"{operation}: {relative}")
            except Exception as exc:  # keep sync robust per file
                result.failed += 1
                error = f"Failed {file_path}: {exc}"
                result.errors.append(error)
                self._log(error)

        missing_files = tree.relative_files - synchronized_files
        missing_folders = tree.relative_directories - synchronized_folders
        missing = sorted(missing_files | missing_folders)
        if missing:
            result.unexplained_omissions = len(missing)
            result.failed += 1
            error = "Completeness check failed for: " + ", ".join(missing)
            result.errors.append(error)
            self._log(error)

        self._log(
            f"Sync complete. files={result.scanned}, folders={result.scanned_folders}, "
            f"uploaded={result.uploaded}, folders_synced={result.folders_synced}, "
            f"skipped={result.skipped}, excluded={result.excluded}, "
            f"failed={result.failed}, omissions={result.unexplained_omissions}"
        )
        return result
