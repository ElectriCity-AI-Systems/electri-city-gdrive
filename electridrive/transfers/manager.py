from __future__ import annotations

import hashlib
import os
from uuid import uuid4
import itertools
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

from electridrive import qa_policy as qa
from electridrive.google_api.client import DriveClientProtocol, RemoteFile
from electridrive.sync.downloader import plan_download
from electridrive.sync.uploader import plan_upload

LOGGER = logging.getLogger(__name__)


class TransferKind(str, Enum):
    UPLOAD = "upload"
    DOWNLOAD = "download"
    EXPORT = "export"


class TransferState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELED = "canceled"


class TransferCanceled(Exception):
    """Raised cooperatively from a progress callback to abort a transfer."""


@dataclass
class Transfer:
    id: int
    kind: TransferKind
    name: str
    total: int = 0
    bytes_done: int = 0
    state: TransferState = TransferState.QUEUED
    error: str = ""
    # endpoints
    local_path: Path | None = None
    dest_path: Path | None = None
    parent_id: str = "root"
    file_id: str = ""
    export_mime: str | None = None
    # timing
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0
    recovered: bool = False
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    @property
    def progress(self) -> float:
        if self.total <= 0:
            return 0.0
        return min(1.0, self.bytes_done / self.total)

    @property
    def is_terminal(self) -> bool:
        return self.state in (TransferState.DONE, TransferState.FAILED, TransferState.CANCELED)

    def speed_bps(self) -> float:
        ref = self.finished_at or time.time()
        elapsed = ref - self.started_at if self.started_at else 0.0
        return (self.bytes_done / elapsed) if elapsed > 0 else 0.0


UpdateCB = Callable[[Transfer], None]
LogCB = Callable[[str], None]


class TransferManager:
    """Threaded upload/download/export queue, independent of any UI toolkit.

    Construct with any object satisfying ``DriveClientProtocol`` (the real
    ``GoogleDriveClient`` or a ``FakeDrive`` in tests). ``on_update`` is invoked
    (possibly from worker threads) whenever a transfer changes; a Qt adapter can
    marshal these onto the GUI thread via signals.
    """

    def __init__(
        self,
        client: DriveClientProtocol,
        *,
        max_workers: int = 3,
        on_update: UpdateCB | None = None,
        on_log: LogCB | None = None,
        journal_path: Path | None = None,
        account_key: str | None = None,
    ):
        qa.check_startup()
        if qa.current_policy() and journal_path is not None:
            raise qa.QaSafetyError("QA safety: filesystem transfer journals are disabled")
        self._client = qa.restrict_client(client)
        self._max_workers = max(1, max_workers)
        self._on_update = on_update
        self._on_log = on_log
        self._queue: "queue.Queue[Transfer | None]" = queue.Queue()
        self._transfers: dict[int, Transfer] = {}
        self._workers: list[threading.Thread] = []
        self._ids = itertools.count(1)
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._outstanding = 0  # queued + running, decremented once at terminal state
        self._started = False
        self._closed = False
        self._planning_cancel = threading.Event()
        self._destinations: set[Path] = set()
        from electridrive.sync.safety import account_key_for
        self._identity = account_key_for(self._client)
        self.account_key = account_key or self._identity
        self._journal = None
        if journal_path is not None:
            from electridrive.transfers.journal import TransferJournal
            self._journal = TransferJournal(journal_path, self.account_key)
            try:
                self._restore()
            except Exception:
                self._journal.close()
                raise


    @staticmethod
    def _payload(transfer: Transfer) -> dict:
        payload = {name: getattr(transfer, name) for name in (
            "name", "total", "bytes_done", "error", "parent_id", "file_id", "export_mime",
            "created_at", "started_at", "finished_at", "recovered")}
        payload.update(kind=transfer.kind.value, state=transfer.state.value,
                       local_path=str(transfer.local_path) if transfer.local_path else None,
                       dest_path=str(transfer.dest_path) if transfer.dest_path else None)
        return payload


    def _restore(self) -> None:
        assert self._journal is not None
        for transfer_id, payload in self._journal.load():
            payload["kind"] = TransferKind(payload["kind"])
            payload["state"] = TransferState(payload["state"])
            for name in ("local_path", "dest_path"):
                payload[name] = Path(payload[name]) if payload.get(name) else None
            transfer = Transfer(id=transfer_id, **payload)
            if not transfer.is_terminal:
                transfer.state = TransferState.FAILED
                transfer.recovered = True
                transfer.finished_at = time.time()
                transfer.error = ("Interrupted upload may already exist on Drive; check the destination "
                                  "before manually retrying." if transfer.kind == TransferKind.UPLOAD else
                                  "Interrupted transfer; retry manually to start it again.")
                self._journal.save(transfer.id, self._payload(transfer))
            self._transfers[transfer.id] = transfer
            if transfer.dest_path is not None:
                self._destinations.add(transfer.dest_path.absolute())


    def _close_journal(self) -> None:
        with self._lock:
            if self._journal is not None:
                self._journal.close()
                self._journal = None

    # ------------------------------------------------------------------ public
    def start(self) -> None:
        with self._lock:
            self._check_open()
            if self._started:
                return
            self._started = True
            for i in range(self._max_workers):
                t = threading.Thread(target=self._worker, name=f"transfer-{i}", daemon=True)
                t.start()
                self._workers.append(t)

    def _check_open(self) -> None:
        if self._closed or self._planning_cancel.is_set():
            raise RuntimeError("Transfer manager is closed")


    def _planning_client(self):
        qa.check_startup()
        manager, client = self, qa.restrict_client(self._client)
        from electridrive.qa_drive import QaDriveClient
        base = QaDriveClient if isinstance(client, QaDriveClient) else object
        class PlanningClient(base):
            def __init__(self):
                pass

            def __getattribute__(self, name):
                attr = getattr(client, name)
                if not callable(attr):
                    return attr
                def call(*args, **kwargs):
                    manager._check_open()
                    qa.check_startup()
                    result = attr(*args, **kwargs)
                    manager._check_open()
                    return result
                return call
        self._check_open()
        return PlanningClient()


    def _reserve_destination(self, path: Path, remote_id: str) -> Path:
        path = Path(path).absolute()
        original = path
        suffix = hashlib.sha256(remote_id.encode()).hexdigest()[:12]
        index = 0
        with self._lock:
            while path in self._destinations or path.exists() or path.is_symlink():
                index += 1
                path = original.with_name(f"{original.stem} [{suffix}-{index}]{original.suffix}")
            self._destinations.add(path)
        return path


    def get(self, transfer_id: int) -> Transfer | None:
        with self._lock:
            return self._transfers.get(transfer_id)


    def replace_client(self, client: DriveClientProtocol) -> None:
        """Refresh grants without losing queue ownership or changing account."""
        from electridrive.sync.safety import account_key_for
        qa.check_startup()
        client = qa.restrict_client(client)
        if account_key_for(client) != self._identity:
            raise ValueError("Transfer client belongs to a different account")
        with self._lock:
            self._check_open()
            self._client = client

    def enqueue_upload(self, local_path: Path, parent_id: str = "root") -> list[int]:
        qa.check_startup()
        qa.require_local(local_path)
        items = plan_upload(self._planning_client(), Path(local_path), parent_id)
        ids: list[int] = []
        for it in items:
            t = self._new_transfer(
                TransferKind.UPLOAD, it.name, total=it.size,
                local_path=it.local_path, parent_id=it.parent_id,
            )
            ids.append(t.id)
            self._enqueue(t)
        self._log(f"Queued {len(ids)} upload(s) from {local_path}")
        self.start()
        return ids

    def enqueue_download(self, remote: RemoteFile, dest_dir: Path) -> list[int]:
        qa.check_startup()
        qa.require_local(dest_dir, exists=False)
        items = plan_download(self._planning_client(), remote, Path(dest_dir))
        ids: list[int] = []
        for it in items:
            kind = TransferKind.EXPORT if it.is_google_doc else TransferKind.DOWNLOAD
            t = self._new_transfer(
                kind, it.name, total=it.size, file_id=it.file_id,
                dest_path=self._reserve_destination(it.dest_path, it.file_id), export_mime=it.export_mime,
            )
            ids.append(t.id)
            self._enqueue(t)
        self._log(f"Queued {len(ids)} download(s) to {dest_dir}")
        self.start()
        return ids

    def _enqueue(self, t: Transfer) -> None:
        with self._cond:
            if self._closed:
                self._finish(t, TransferState.CANCELED)
                return
            if not t.is_terminal:
                self._queue.put(t)

    def cancel(self, transfer_id: int) -> None:
        with self._lock:
            t = self._transfers.get(transfer_id)
        if t and not t.is_terminal:
            t._cancel.set()
            if t.state == TransferState.QUEUED:
                # Not yet picked up: mark canceled now (a running task is aborted
                # cooperatively via the progress callback instead).
                self._finish(t, TransferState.CANCELED)

    def cancel_pending(self) -> None:
        """Stop planners immediately while session unmount/drain is attempted."""
        self._planning_cancel.set()
        self.cancel_all()

    def cancel_all(self) -> None:
        with self._lock:
            ids = list(self._transfers)
        for tid in ids:
            self.cancel(tid)

    def retry(self, transfer_id: int) -> int | None:
        with self._lock:
            old = self._transfers.get(transfer_id)
            self._check_open()
            if not old or old.state != TransferState.FAILED:
                return None
            self._validate_transfer(old)
            new = self._new_transfer(
                old.kind, old.name, total=old.total, local_path=old.local_path,
                dest_path=old.dest_path, parent_id=old.parent_id, file_id=old.file_id,
                export_mime=old.export_mime,
            )
        self._enqueue(new)
        self.start()
        return new.id

    def snapshot(self) -> list[Transfer]:
        with self._lock:
            return list(self._transfers.values())

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for t in self._transfers.values() if not t.is_terminal)

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Block until every enqueued transfer has reached a terminal state."""
        deadline = None if timeout is None else time.time() + timeout
        with self._cond:
            while self._outstanding > 0:
                remaining = None if deadline is None else max(0.0, deadline - time.time())
                if remaining == 0.0:
                    return False
                self._cond.wait(timeout=remaining)
            return True

    def shutdown(self, wait: bool = True) -> None:
        with self._lock:
            already_closed = self._closed
            self._closed = True
            self._planning_cancel.set()
        if already_closed:
            if wait:
                for worker in self._workers:
                    worker.join()
                self._close_journal()
            return
        self.cancel_all()
        for _ in self._workers:
            self._queue.put(None)
        if wait:
            for w in self._workers:
                w.join()
            self._close_journal()
        elif not self._workers:
            self._close_journal()
        elif self._journal is not None:
            def reap():
                for worker in self._workers:
                    worker.join()
                self._close_journal()
            threading.Thread(target=reap, name="transfer-journal-close", daemon=True).start()

    # ----------------------------------------------------------------- internal
    def _new_transfer(self, kind: TransferKind, name: str, **kw) -> Transfer:
        with self._lock:
            self._check_open()
            t = Transfer(id=next(self._ids), kind=kind, name=name, **kw)
            if self._journal is not None:
                t.id = self._journal.allocate(self._payload(t))
            self._transfers[t.id] = t
            self._outstanding += 1
        self._emit(t)
        return t

    def _worker(self) -> None:
        while True:
            t = self._queue.get()
            try:
                if t is None:
                    return
                if t._cancel.is_set():
                    self._finish(t, TransferState.CANCELED)
                    continue
                self._run(t)
            finally:
                self._queue.task_done()

    def _validate_transfer(self, t: Transfer):
        qa.check_startup()
        client = qa.restrict_client(self._client)
        if t.kind == TransferKind.UPLOAD:
            qa.require_local(t.local_path)
            if qa.current_policy():
                client.get_metadata(t.parent_id)
        else:
            qa.require_local(t.dest_path, exists=False)
            if qa.current_policy():
                client.get_metadata(t.file_id)

        return client

    def _run(self, t: Transfer) -> None:
        with self._cond:
            if t.is_terminal:  # canceled while still queued
                return
            t.state = TransferState.RUNNING
        t.started_at = time.time()

        def progress(done: int, total: int) -> None:
            if t._cancel.is_set():
                raise TransferCanceled()
            t.bytes_done = done
            if total:
                t.total = total
            self._emit(t)

        try:
            if not self._emit(t):
                raise RuntimeError("Transfer journal is unavailable; request was not started")
            client = self._validate_transfer(t)
            if t._cancel.is_set():
                raise TransferCanceled()
            if t.kind == TransferKind.UPLOAD:
                assert t.local_path is not None
                from electridrive.sync.safety import opened_source
                source = t.local_path.expanduser().absolute()
                anchor = Path(source.anchor)
                relative = source.relative_to(anchor).as_posix()
                # Pin every ancestor and the file itself without following symlinks.
                # The descriptor remains open while the adapter consumes its bytes.
                with opened_source(anchor, relative) as pinned:
                    t.file_id = client.upload_file(pinned, t.parent_id, t.name, progress)
                self._emit(t)  # retain a returned remote ID even before terminal publication
            elif t.kind == TransferKind.DOWNLOAD:
                assert t.dest_path is not None
                self._download_atomic(client, t, progress)
            elif t.kind == TransferKind.EXPORT:
                assert t.dest_path is not None
                self._download_atomic(client, t, progress)
            t.bytes_done = t.total or t.bytes_done
            t.finished_at = time.time()
            self._finish(t, TransferState.DONE)
            self._log(f"{t.kind.value} done: {t.name}")
        except TransferCanceled:
            t.finished_at = time.time()
            self._finish(t, TransferState.CANCELED)
            self._log(f"{t.kind.value} canceled: {t.name}")
        except Exception as exc:  # keep the queue alive; isolate per-task failures
            t.error = str(exc)
            t.finished_at = time.time()
            self._finish(t, TransferState.FAILED)
            LOGGER.exception("Transfer failed: %s", t.name)
            self._log(f"{t.kind.value} FAILED: {t.name}: {exc}")

    def _download_atomic(self, client, transfer: Transfer, progress) -> None:
        from electridrive.sync.safety import anchored_parent
        dest = transfer.dest_path.expanduser().absolute()
        root = Path(dest.anchor)
        relative = dest.relative_to(root).as_posix()
        with anchored_parent(root, relative, create=True) as (parent_fd, name):
            temporary = ".electridrive-" + uuid4().hex
            partial = dest.with_name(temporary)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=parent_fd)
            os.close(fd)
            try:
                qa.require_local(partial, exists=False)
                if transfer.kind == TransferKind.EXPORT:
                    client.export_file(transfer.file_id, partial, transfer.export_mime, progress)
                else:
                    client.download_file(transfer.file_id, partial, progress)
                if transfer._cancel.is_set():
                    raise TransferCanceled()
                qa.check_startup()
                qa.require_local(dest, exists=False)
                # Reject renamed/replaced ancestors before linking into the held directory.
                with anchored_parent(root, relative) as (current_fd, _):
                    held, current = os.fstat(parent_fd), os.fstat(current_fd)
                    if (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino):
                        raise ValueError("Download parent directory changed")
                fd = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
                with os.fdopen(fd, "rb") as stream:
                    os.fsync(stream.fileno())
                    transfer.bytes_done = os.fstat(stream.fileno()).st_size
                os.link(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
                        follow_symlinks=False)
                os.fsync(parent_fd)
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass

    def _finish(self, t: Transfer, state: TransferState) -> None:
        """Set a terminal state exactly once and release waiters."""
        with self._cond:
            if t.is_terminal:
                return
            t.state = state
            self._outstanding -= 1
            self._cond.notify_all()
        self._emit(t)

    def _emit(self, t: Transfer) -> bool:
        durable = True
        try:
            with self._lock:
                if self._journal is not None:
                    self._journal.save(t.id, self._payload(t))
        except Exception as exc:
            durable = False
            LOGGER.exception("Could not save transfer %s", t.id)
            self._log(f"Could not save transfer {t.id}: {exc}")
        if self._on_update:
            try:
                self._on_update(t)
            except Exception:  # a broken UI callback must not kill a worker
                LOGGER.debug("on_update callback raised", exc_info=True)
        return durable

    def _log(self, message: str) -> None:
        LOGGER.info(message)
        if self._on_log:
            try:
                self._on_log(message)
            except Exception:
                LOGGER.debug("on_log callback raised", exc_info=True)
