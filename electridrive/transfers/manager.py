from __future__ import annotations

import itertools
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

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
    ):
        self._client = client
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

    # ------------------------------------------------------------------ public
    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            for i in range(self._max_workers):
                t = threading.Thread(target=self._worker, name=f"transfer-{i}", daemon=True)
                t.start()
                self._workers.append(t)

    def enqueue_upload(self, local_path: Path, parent_id: str = "root") -> list[int]:
        items = plan_upload(self._client, Path(local_path), parent_id)
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
        items = plan_download(self._client, remote, Path(dest_dir))
        ids: list[int] = []
        for it in items:
            kind = TransferKind.EXPORT if it.is_google_doc else TransferKind.DOWNLOAD
            t = self._new_transfer(
                kind, it.name, total=it.size, file_id=it.file_id,
                dest_path=it.dest_path, export_mime=it.export_mime,
            )
            ids.append(t.id)
            self._enqueue(t)
        self._log(f"Queued {len(ids)} download(s) to {dest_dir}")
        self.start()
        return ids

    def _enqueue(self, t: Transfer) -> None:
        with self._cond:
            self._outstanding += 1
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

    def cancel_all(self) -> None:
        with self._lock:
            ids = list(self._transfers)
        for tid in ids:
            self.cancel(tid)

    def retry(self, transfer_id: int) -> int | None:
        with self._lock:
            old = self._transfers.get(transfer_id)
            if not old or old.state != TransferState.FAILED:
                return None
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
        for _ in self._workers:
            self._queue.put(None)
        if wait:
            for w in self._workers:
                w.join(timeout=5.0)

    # ----------------------------------------------------------------- internal
    def _new_transfer(self, kind: TransferKind, name: str, **kw) -> Transfer:
        with self._lock:
            t = Transfer(id=next(self._ids), kind=kind, name=name, **kw)
            self._transfers[t.id] = t
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

    def _run(self, t: Transfer) -> None:
        with self._cond:
            if t.is_terminal:  # canceled while still queued
                return
            t.state = TransferState.RUNNING
        t.started_at = time.time()
        self._emit(t)

        def progress(done: int, total: int) -> None:
            if t._cancel.is_set():
                raise TransferCanceled()
            t.bytes_done = done
            if total:
                t.total = total
            self._emit(t)

        try:
            if t.kind == TransferKind.UPLOAD:
                assert t.local_path is not None
                t.file_id = self._client.upload_file(t.local_path, t.parent_id, t.name, progress)
            elif t.kind == TransferKind.DOWNLOAD:
                assert t.dest_path is not None
                self._client.download_file(t.file_id, t.dest_path, progress)
            elif t.kind == TransferKind.EXPORT:
                assert t.dest_path is not None
                self._client.export_file(t.file_id, t.dest_path, t.export_mime, progress)
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

    def _finish(self, t: Transfer, state: TransferState) -> None:
        """Set a terminal state exactly once and release waiters."""
        with self._cond:
            if t.is_terminal:
                return
            t.state = state
            self._outstanding -= 1
            self._cond.notify_all()
        self._emit(t)

    def _emit(self, t: Transfer) -> None:
        if self._on_update:
            try:
                self._on_update(t)
            except Exception:  # a broken UI callback must not kill a worker
                LOGGER.debug("on_update callback raised", exc_info=True)

    def _log(self, message: str) -> None:
        LOGGER.info(message)
        if self._on_log:
            try:
                self._on_log(message)
            except Exception:
                LOGGER.debug("on_log callback raised", exc_info=True)
