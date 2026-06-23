from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from electridrive.google_api.client import DriveClientProtocol
from electridrive.storage.database import SyncDatabase
from electridrive.sync.hasher import sha256_file
from electridrive.sync.rules import SyncRules

LOGGER = logging.getLogger(__name__)
ProgressCallback = Callable[[str, int, int], None]
LogCallback = Callable[[str], None]


@dataclass
class SyncResult:
    scanned: int = 0
    uploaded: int = 0
    skipped: int = 0
    excluded: int = 0
    failed: int = 0
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
        root = root.expanduser().resolve()
        for path in root.rglob("*"):
            if self.rules.is_excluded(path, root):
                continue
            if path.is_file():
                yield path

    def should_upload(self, file_path: Path, sha256: str) -> bool:
        stat = file_path.stat()
        record = self.database.get_file(str(file_path))
        if record is None:
            return True
        return not (record.size == stat.st_size and record.mtime_ns == stat.st_mtime_ns and record.sha256 == sha256)

    def sync_up(self, local_root: Path, remote_folder: str) -> SyncResult:
        result = SyncResult()
        local_root = local_root.expanduser().resolve()
        if not local_root.exists() or not local_root.is_dir():
            raise ValueError(f"Local root does not exist or is not a folder: {local_root}")

        self._log(f"Starting upload-only sync: {local_root} -> {remote_folder}")
        root_remote_id = self.drive_client.ensure_folder_path(remote_folder)
        files = list(self.iter_candidate_files(local_root))
        total = len(files)

        for index, file_path in enumerate(files, start=1):
            result.scanned += 1
            if self.progress_callback:
                self.progress_callback(str(file_path), index, total)
            try:
                digest = sha256_file(file_path)
                if not self.should_upload(file_path, digest):
                    result.skipped += 1
                    self._log(f"Skipped unchanged: {file_path.relative_to(local_root)}")
                    continue

                relative = file_path.relative_to(local_root)
                parent_relative = relative.parent
                if str(parent_relative) in {".", ""}:
                    parent_id = root_remote_id
                    remote_path = f"{remote_folder}/{relative.name}"
                else:
                    folder_path = f"{remote_folder}/{parent_relative.as_posix()}"
                    parent_id = self.drive_client.ensure_folder_path(folder_path)
                    self.database.upsert_folder(
                        local_rel_path=parent_relative.as_posix(),
                        remote_path=folder_path,
                        remote_id=parent_id,
                    )
                    remote_path = f"{folder_path}/{relative.name}"

                remote_id = self.drive_client.upload_file(file_path, parent_id, file_path.name)
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
                self._log(f"Uploaded: {relative}")
            except Exception as exc:  # keep sync robust per file
                result.failed += 1
                error = f"Failed {file_path}: {exc}"
                result.errors.append(error)
                self._log(error)

        self._log(
            f"Sync complete. scanned={result.scanned}, uploaded={result.uploaded}, "
            f"skipped={result.skipped}, failed={result.failed}"
        )
        return result
