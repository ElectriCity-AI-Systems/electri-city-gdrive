from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from electridrive.config import Settings, get_paths, load_settings, save_settings
from electridrive.storage.database import SyncDatabase

LOGGER = logging.getLogger(__name__)


class _WorkerSignals(QObject):
    result = Signal(object)
    error = Signal(str)


class _Worker(QRunnable):
    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self.signals = _WorkerSignals()

    @Slot()
    def run(self):
        try:
            result = self._fn(*self._args, **self._kwargs)
        except Exception as exc:  # surfaced to the UI via the error signal
            LOGGER.exception("Background task failed")
            self.signals.error.emit(str(exc))
        else:
            self.signals.result.emit(result)


class DriveSession(QObject):
    """Owns auth, the Drive client, the transfer manager and the local DB.

    Network/auth work runs in a thread pool; results arrive via Qt signals so the
    GUI thread never blocks.
    """

    connected = Signal(object)        # StorageQuota
    connect_failed = Signal(str)
    transfer_changed = Signal(object)  # Transfer
    log = Signal(str)

    def __init__(self):
        super().__init__()
        self.paths = get_paths()
        self.settings: Settings = load_settings()
        self.db = SyncDatabase(self.paths.database_file)
        self.pool = QThreadPool.globalInstance()
        self.client = None
        self.transfers = None
        self.about = None
        self.fuse_mount = None
        self._pending: set[_Worker] = set()

    # ----------------------------------------------------------------- helpers
    @property
    def is_connected(self) -> bool:
        return self.client is not None

    def has_credentials(self) -> bool:
        return self.paths.credentials_file.exists()

    def has_token(self) -> bool:
        # A cached token (file fallback) means we can likely auto-connect silently.
        return self.paths.token_fallback_file.exists()

    def submit(self, fn, on_result=None, on_error=None, *args, **kwargs):
        # Keep the worker (and its signals QObject) alive until the queued result is
        # delivered; otherwise QThreadPool auto-deletes the runnable first and the
        # cross-thread signal is silently dropped.
        worker = _Worker(fn, *args, **kwargs)
        worker.setAutoDelete(False)
        self._pending.add(worker)
        if on_result:
            worker.signals.result.connect(on_result)
        if on_error:
            worker.signals.error.connect(on_error)
        worker.signals.result.connect(lambda *_: self._pending.discard(worker))
        worker.signals.error.connect(lambda *_: self._pending.discard(worker))
        self.pool.start(worker)

    def save(self):
        save_settings(self.settings)

    # ------------------------------------------------------------------ connect
    def connect_async(self):
        self.submit(self._do_connect, on_result=self._on_connected,
                    on_error=self.connect_failed.emit)

    def _do_connect(self):
        from electridrive.google_api.client import GoogleDriveClient

        client = GoogleDriveClient()  # triggers OAuth (or refresh) as needed
        about = client.get_about()
        return client, about

    def _on_connected(self, payload):
        from electridrive.transfers import TransferManager

        client, about = payload
        self.client = client
        self.about = about
        self.transfers = TransferManager(
            client,
            on_update=lambda t: self.transfer_changed.emit(t),
            on_log=lambda m: self.log.emit(m),
        )
        self.log.emit(f"Connected as {about.user_email or 'Drive user'}")
        self.connected.emit(about)

    def refresh_about(self, on_result=None):
        if not self.client:
            return
        self.submit(self.client.get_about, on_result=on_result)

    def reload_client(self):
        """Rebuild the client + transfer manager from the saved token (e.g. after the
        Picker grants access to new files). No browser — the token is already valid."""
        from electridrive.google_api.client import GoogleDriveClient
        from electridrive.transfers import TransferManager

        old = self.transfers
        self.client = GoogleDriveClient()
        self.transfers = TransferManager(
            self.client,
            on_update=lambda t: self.transfer_changed.emit(t),
            on_log=lambda m: self.log.emit(m),
        )
        # Picker grants may not surface through the Drive changes() feed; drop the
        # remote cache + tokens so the next sync re-walks and re-seeds (GUARD 0).
        self.db.clear_remote_cache()
        if old:
            old.shutdown(wait=False)

    def set_access_mode(self, mode: str):
        """Switch between 'file' (drive.file) and 'drive' (Pro/full). Clears the saved
        token so the next connect re-consents with the new scope."""
        from electridrive.google_api.oauth import clear_credentials

        self.settings.scope = mode
        self.save()
        clear_credentials()
        # The change feed is scope-sensitive (drive.file vs full drive see different
        # files); a token/cache from the old scope must not be trusted under the new one.
        self.db.clear_remote_cache()
        self.client = None
        self.about = None

    def pick_from_drive(self, on_done=None, on_error=None):
        """Open the desktop Drive Picker (in a worker thread), then refresh access."""
        def work():
            from electridrive.google_api.picker import pick_from_drive as _pick
            return _pick()

        def done(result):
            self.reload_client()
            self.log.emit(f"Added {len(result.file_ids)} item(s) from Drive")
            if on_done:
                on_done(result)

        self.submit(work, done, on_error)

    def shutdown(self):
        try:
            if self.fuse_mount and self.fuse_mount.is_mounted:
                self.fuse_mount.stop()
        except Exception:
            LOGGER.debug("fuse unmount on shutdown failed", exc_info=True)
        try:
            if self.transfers:
                self.transfers.cancel_all()
                self.transfers.shutdown(wait=False)
        finally:
            self.db.close()
