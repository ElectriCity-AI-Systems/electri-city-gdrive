from __future__ import annotations

from electridrive import qa_policy as qa

import logging
import hashlib
import inspect
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from electridrive.google_api.client import DriveClientProtocol
from electridrive.storage.database import SyncDatabase
from electridrive.sync.hasher import sha256_file
from electridrive.sync.rules import SyncRules
from electridrive.sync.tree import canonical_sync_root, safe_local_path, scan_local_tree

from electridrive.sync.safety import (SyncCancelled, check_cancelled, pair_lock, account_key_for, opened_source)

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
        cancel_event=None,
        confirmed_remote_ids: dict[str, str] | None = None,
    ):
        self.cancel_event = cancel_event
        self._file_scope = ""
        self.drive_client = drive_client
        self.database = database
        self.rules = rules if rules is not None else SyncRules.full_backup()
        self.log_callback = log_callback
        self.progress_callback = progress_callback
        # Explicit, caller-reviewed {relative_path: remote_id} mapping -- the
        # only way an existing, untracked Drive file may be adopted on this
        # pair's first sync. A missing database record does not prove a
        # Drive file belongs to this job, so nothing is adopted without it.
        self.confirmed_remote_ids = dict(confirmed_remote_ids or {})

    def _transfer(self, method, *args):
        """Older small adapters may not expose chunk progress callbacks."""
        parameters = inspect.signature(method).parameters.values()
        progress = any(item.name == "progress_cb" or item.kind == inspect.Parameter.VAR_KEYWORD
                       for item in parameters)
        kwargs = {"progress_cb": lambda *_: check_cancelled(self.cancel_event)} if progress else {}
        check_cancelled(self.cancel_event)
        result = method(*args, **kwargs)
        check_cancelled(self.cancel_event)
        return result

    def _log(self, message: str) -> None:
        LOGGER.info(message)
        if self.log_callback:
            self.log_callback(message)

    def iter_candidate_files(self, root: Path) -> Iterable[Path]:
        qa.check_startup()
        inventory = scan_local_tree(root, self.rules)
        if inventory.errors and self.rules.fail_on_unsupported:
            raise RuntimeError(
                "Incomplete full-backup inventory: " + "; ".join(inventory.errors)
            )
        yield from inventory.files

    def should_upload(self, file_path: Path, sha256: str) -> bool:
        qa.require_local(file_path)
        stat = file_path.stat()
        record = self.database.get_file(str(file_path), scope=self._file_scope)
        if record is None:
            return True
        return not (record.size == stat.st_size and record.mtime_ns == stat.st_mtime_ns and record.sha256 == sha256)

    def sync_up(self, local_root: Path, remote_folder: str) -> SyncResult:
        check_cancelled(self.cancel_event)
        with pair_lock(Path(local_root).expanduser().absolute()):
            return self._sync_up_locked(local_root, remote_folder)

    def _sync_up_locked(self, local_root: Path, remote_folder: str) -> SyncResult:
        qa.check_startup()
        from electridrive.config import SyncPair
        qa.validate_pair(SyncPair(str(local_root), remote_folder, direction="up_only", delete_policy="off"))
        self.drive_client = qa.restrict_client(self.drive_client)
        result = SyncResult()
        local_root = canonical_sync_root(local_root)

        self._log(f"Starting upload-only sync: {local_root} -> {remote_folder}")
        # Verify the whole local inventory before any remote mutation. In full
        # backup mode unsupported links/special files must fail visibly, not
        # silently yield a misleading "complete" result.
        tree = scan_local_tree(local_root, self.rules)
        files = list(tree.files)
        result.scanned_folders = len(tree.directories)
        result.excluded = len(tree.excluded)
        for error in tree.errors:
            result.failed += 1
            result.errors.append(error)
            self._log(f"Local traversal failed: {error}")
        if tree.errors:
            return result
        check_cancelled(self.cancel_event)
        root_remote_id = self.drive_client.ensure_folder_path(remote_folder)
        identity = local_root.stat()
        scope = json.dumps([account_key_for(self.drive_client), str(local_root),
                            identity.st_dev, identity.st_ino, remote_folder, root_remote_id])
        self._file_scope = hashlib.sha256(scope.encode()).hexdigest()

        synchronized_folders: set[str] = set()
        remote_directories: dict[str, str] = {"": root_remote_id}
        for directory in sorted(
            tree.directories,
            key=lambda path: (len(path.relative_to(local_root).parts), path.as_posix()),
        ):
            check_cancelled(self.cancel_event)
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
            check_cancelled(self.cancel_event)
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

                previous = self.database.get_file(str(file_path), scope=self._file_scope)
                finder = getattr(self.drive_client, "find_file", None)
                found_id = finder(file_path.name, parent_id) if callable(finder) else None

                # A name match is not ownership, and a missing database record does
                # not prove a Drive file belongs to this job. Only this pair's own
                # previously recorded remote_id, or an explicit, caller-reviewed
                # entry in confirmed_remote_ids, may be trusted as an update target.
                # An existing Drive file with neither is never silently adopted.
                if previous is None:
                    remote_id = None
                    if found_id is not None:
                        confirmed = self.confirmed_remote_ids.get(relative.as_posix())
                        if confirmed != found_id:
                            raise RuntimeError(
                                f"an existing Drive file named {file_path.name!r} in this folder "
                                "is not confirmed as belonging to this sync pair; review its "
                                "identity and pass it via confirmed_remote_ids to adopt it "
                                "(rename/move it, or resolve the conflict manually)"
                            )
                        remote_id = found_id
                else:
                    remote_id = previous.remote_id
                    if callable(finder) and found_id is not None and found_id != remote_id:
                        verify = getattr(self.drive_client, "get_metadata", None)
                        alive = False
                        if callable(verify):
                            try:
                                alive = not verify(remote_id).trashed
                            except Exception:
                                alive = False
                        if not alive:
                            raise RuntimeError(
                                f"stale Drive ID for {relative}: the previously tracked file "
                                f"{remote_id!r} is gone and a different, unrecognized file now "
                                "uses this name; refusing to overwrite it without confirmation"
                            )
                        # Keep targeting our own tracked file; ignore the unrelated name match.
                        found_id = remote_id

                if (
                    previous is not None
                    and not self.should_upload(file_path, digest)
                    and (not callable(finder) or found_id == remote_id)
                ):
                    result.skipped += 1
                    synchronized_files.add(relative.as_posix())
                    self._log(f"Skipped unchanged: {relative}")
                    continue

                with opened_source(local_root, relative.as_posix()) as source:
                    stat = source.stat()
                    digest = sha256_file(source)
                    check_cancelled(self.cancel_event)
                    if remote_id is None:
                        remote_id = self._transfer(self.drive_client.upload_file, source, parent_id, file_path.name)
                        operation = "Uploaded"
                    else:
                        self._transfer(self.drive_client.update_file, remote_id, source)
                        operation = "Updated"
                    check_cancelled(self.cancel_event)
                self.database.upsert_file(
                    local_path=str(file_path),
                    remote_id=remote_id,
                    remote_path=remote_path,
                    size=stat.st_size,
                    mtime_ns=stat.st_mtime_ns,
                    sha256=digest,
                    status="uploaded",
                    scope=self._file_scope,
                )
                self.database.upsert_file(
                    local_path=str(file_path), remote_id=remote_id, remote_path=remote_path,
                    size=stat.st_size, mtime_ns=stat.st_mtime_ns, sha256=digest, status="uploaded",
                )
                result.uploaded += 1
                synchronized_files.add(relative.as_posix())
                self._log(f"{operation}: {relative}")
            except SyncCancelled:
                raise
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
