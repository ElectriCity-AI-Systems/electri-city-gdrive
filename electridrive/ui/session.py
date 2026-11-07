from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait
import itertools
import threading
import logging

from PySide6.QtCore import QObject, Signal, Slot

from electridrive import qa_policy as qa
from electridrive.config import Settings, get_paths, load_settings, save_settings
from electridrive.storage.database import SyncDatabase

LOGGER = logging.getLogger(__name__)
_WORKER_IDS = itertools.count(1)


class _WorkerSignals(QObject):
    finished = Signal(object, object, object)
    result = Signal(object)
    error = Signal(str)


class _Worker:
    def __init__(self, fn, *args, **kwargs):
        self.worker_id = next(_WORKER_IDS)
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self.signals = _WorkerSignals()

    @Slot()
    def run(self):
        result, error = None, None
        try:
            qa.check_startup()
            result = self._fn(*self._args, **self._kwargs)
        except Exception as exc:  # surfaced to the UI via the error signal
            qa.log_exception(LOGGER, "Background task failed")
            error = str(exc)
            self.signals.error.emit(error)
        else:
            self.signals.result.emit(result)
        finally:
            self.signals.finished.emit(self.worker_id, result, error)


class _WorkerPool:
    """Python workers avoid Qt's retained QRunnable ownership after completion."""

    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="drive-session")
        self._futures = set()
        self._lock = threading.Lock()

    def start(self, worker):
        with self._lock:
            self._futures = {future for future in self._futures if not future.done()}
            self._futures.add(self._executor.submit(worker.run))

    def waitForDone(self, milliseconds=-1):
        with self._lock:
            futures = tuple(self._futures)
        _done, pending = wait(futures, timeout=None if milliseconds < 0 else milliseconds / 1000)
        with self._lock:
            self._futures.difference_update(_done)
        return not pending

    def shutdown(self):
        self._executor.shutdown(wait=True)


class DriveSession(QObject):
    """Own account resources and drain all jobs before releasing their session."""

    connected = Signal(object)
    connect_failed = Signal(str)
    transfer_changed = Signal(object)
    _transfer_update = Signal(object, int)
    log = Signal(str)

    def __init__(self):
        super().__init__()
        qa.check_startup()
        self.paths = get_paths()
        self.settings: Settings = load_settings()
        self.db = SyncDatabase(self.paths.database_file)
        self.pool = _WorkerPool()
        self.client = None
        self.transfers = None
        self.about = None
        self.fuse_mount = None
        self._pending: set[_Worker] = set()
        self._callbacks = {}
        self._generation = 0
        self._accepting = True
        self._closed = False
        self.account_key = None
        self._transfer_update.connect(self._deliver_transfer)
        from electridrive.ui.sync_state import SyncState
        self.sync_state = SyncState()

    # ----------------------------------------------------------------- helpers
    @property
    def is_connected(self) -> bool:
        return self.client is not None

    def has_credentials(self) -> bool:
        return self.paths.credentials_file.exists()

    def has_token(self) -> bool:
        from electridrive.google_api.oauth import OAuthError, has_saved_token
        try:
            return has_saved_token()
        except OAuthError as exc:
            LOGGER.warning("Saved credentials are unavailable: %s", exc)
            self.log.emit(str(exc))
            return False

    def submit(self, fn, on_result=None, on_error=None, *args, **kwargs):
        if not self._accepting or self._closed:
            raise RuntimeError("Session is closing; new work is refused")
        generation = self._generation
        def work():
            if generation != self._generation:
                return None
            return fn(*args, **kwargs)
        worker = _Worker(work)
        # Retain signal owners until queued delivery, then disconnect and release them.
        self._pending.add(worker)
        self._callbacks[worker.worker_id] = (worker, generation, on_result, on_error)
        worker.signals.finished.connect(self._finish_worker)
        try:
            self.pool.start(worker)
        except Exception:
            self._callbacks.pop(worker.worker_id, None)
            self._pending.discard(worker)
            worker.signals.finished.disconnect(self._finish_worker)
            worker.signals.deleteLater()
            raise
        return worker

    @Slot(object, object, object)
    def _finish_worker(self, worker_id, result, error):
        entry = self._callbacks.pop(worker_id, None)
        if entry is None:
            return
        worker, generation, on_result, on_error = entry
        self._pending.discard(worker)
        worker.signals.finished.disconnect(self._finish_worker)
        worker.signals.deleteLater()
        if generation != self._generation or self._closed:
            return
        callback = on_error if error is not None else on_result
        if callback:
            from shiboken6 import isValid
            owner = getattr(callback, "__self__", None)
            if isinstance(owner, QObject) and not isValid(owner):
                return
            callback(error if error is not None else result)

    def save(self):
        qa.validate_settings(self.settings)
        save_settings(self.settings)

    # ------------------------------------------------------------------ connect
    def connect_async(self):
        self.submit(self._do_connect, on_result=self._on_connected,
                    on_error=self.connect_failed.emit)

    def _do_connect(self):
        from electridrive.google_api.client import GoogleDriveClient
        from electridrive.sync.safety import account_key_for
        client = qa.restrict_client(GoogleDriveClient())
        about = client.get_about()
        account_key_for(client)  # prime immutable identity in this background job
        return client, about

    @Slot(object, int)
    def _deliver_transfer(self, transfer, generation):
        if generation == self._generation and not self._closed:
            self.transfer_changed.emit(transfer)

    def _on_connected(self, payload):
        from electridrive.transfers import TransferManager
        from electridrive.sync.safety import account_key_for
        client, about = payload
        try:
            qa.check_startup()
            client = qa.restrict_client(client)
            identity = account_key_for(client)
            if self.transfers is not None and identity != self.account_key:
                raise RuntimeError("Drive account changed; sign out before switching account")
            manager = self.transfers
            if manager is None:
                generation = self._generation
                manager = TransferManager(client,
                    on_update=lambda t: self._transfer_update.emit(t, generation),
                    on_log=self.log.emit,
                    journal_path=(None if qa.current_policy() or identity.startswith("ephemeral:")
                                  else self.paths.state_dir / "transfer_queue.sqlite3"),
                    account_key=identity)
            else:
                manager.replace_client(client)
        except Exception as exc:
            qa.log_exception(LOGGER, "Could not open account transfer queue")
            self.connect_failed.emit(str(exc))
            return False
        self.account_key = identity
        self.client, self.about, self.transfers = client, about, manager
        self.log.emit(f"Connected as {about.user_email or 'Drive user'}")
        self.connected.emit(about)
        return True

    def refresh_about(self, on_result=None):
        if not self.client:
            return
        self.submit(self.client.get_about, on_result=on_result)

    def reload_client(self, on_done=None, on_error=None):
        """Refresh grants in the background, preserving the manager and transfer IDs."""
        expected = self.account_key
        def work():
            from electridrive.sync.safety import account_key_for
            payload = self._do_connect()
            if expected and account_key_for(payload[0]) != expected:
                raise RuntimeError("Picker account changed; sign out before switching account")
            return payload
        def done(payload):
            if not self._on_connected(payload):
                return
            if on_done:
                on_done(payload[1])
        return self.submit(work, done, on_error or self.log.emit)

    def _drain(self):
        self._accepting = False
        self._generation += 1
        self.sync_state.cancel.set()
        if self.transfers:
            self.transfers.cancel_pending()
        if self.fuse_mount and self.fuse_mount.is_mounted:
            self.fuse_mount.stop()  # retain references and report failure if still mounted
        if self.transfers:
            self.transfers.shutdown(wait=True)
        self.pool.waitForDone()
        # Delivery can still be queued; disconnect it and release every retained closure.
        for worker in tuple(self._pending):
            worker.signals.finished.disconnect(self._finish_worker)
            worker.signals.deleteLater()
        self._pending.clear()
        self._callbacks.clear()
        self.sync_state.reset()

    def sign_out(self):
        from electridrive.google_api.oauth import clear_credentials
        self._drain()
        clear_credentials()
        self.client = self.about = self.transfers = self.fuse_mount = None
        self.account_key = None
        self._accepting = True

    def set_access_mode(self, mode: str):
        """Switch between 'file' (drive.file) and 'drive' (Pro/full). Clears the saved
        token so the next connect re-consents with the new scope."""
        if qa.current_policy() and mode != "file":
            raise qa.QaSafetyError("QA safety: only per-file access is supported")
        self.sign_out()
        self.settings.scope = mode
        self.save()

    def pick_from_drive(self, on_done=None, on_error=None):
        expected = self.account_key
        def work():
            if qa.current_policy():
                raise qa.QaSafetyError("QA safety: Drive Picker is disabled; use registered QA roots")
            from electridrive.google_api.client import GoogleDriveClient
            from electridrive.google_api.picker import pick_from_drive
            from electridrive.sync.safety import account_key_for
            result = pick_from_drive(persist=False)
            client = qa.restrict_client(GoogleDriveClient(credentials=result.credentials))
            about = client.get_about()
            if not expected or account_key_for(client) != expected:
                raise RuntimeError("Picker account changed; sign out before switching account")
            return result, client, about
        def done(payload):
            from electridrive.google_api.oauth import save_credentials
            result, client, about = payload
            if not self._on_connected((client, about)):
                return
            save_credentials(result.credentials)
            self.log.emit(f"Added {len(result.file_ids)} item(s) from Drive")
            if on_done:
                on_done(result)
        self.submit(work, done, on_error)

    def shutdown(self):
        if self._closed:
            return
        self._drain()
        self.pool.shutdown()
        self.db.close()
        self._closed = True
