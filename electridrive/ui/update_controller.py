"""Qt presentation/controller layer for the independent update checker."""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMessageBox, QWidget

from electridrive.config import APP_NAME, APP_VERSION
from electridrive.updater.update_checker import (
    CheckResult,
    CheckStatus,
    ReleaseInfo,
    UpdateCheckService,
)

LOGGER = logging.getLogger(__name__)


class UpdateController(QObject):
    """Run update checks off the GUI thread and present native dialogs."""

    checking_changed = Signal(bool)

    def __init__(self, session, parent: QWidget):
        super().__init__(parent)
        self._session = session
        self._parent = parent
        self._service = UpdateCheckService(
            session.settings,
            session.save,
            current_version=APP_VERSION,
        )
        self._checking = False
        self._pending_manual_check = False

    @property
    def is_checking(self) -> bool:
        return self._checking

    def start_automatic_check(self) -> None:
        if self._checking or not self._service.automatic_check_is_due():
            return
        self._start(manual=False)

    def check_manually(self) -> None:
        if self._checking:
            # An explicit request must not be lost just because the quiet startup
            # check is still finishing.
            self._pending_manual_check = True
            return
        self._start(manual=True)

    def _start(self, *, manual: bool) -> None:
        self._checking = True
        self.checking_changed.emit(True)
        self._session.submit(
            lambda: self._service.check(manual=manual),
            on_result=lambda result: self._finished(result, manual=manual),
            on_error=lambda message: self._worker_failed(message, manual=manual),
        )

    def _finished(self, result: CheckResult, *, manual: bool) -> None:
        self._checking = False
        self.checking_changed.emit(False)
        if result.status is CheckStatus.AVAILABLE and result.release is not None:
            self._show_available(result.release)
            # If the user clicked the manual action while the automatic request was
            # in flight, this dialog already answers that request; avoid showing the
            # same release twice in succession.
            self._pending_manual_check = False
        elif manual and result.status is CheckStatus.UP_TO_DATE:
            QMessageBox.information(
                self._parent,
                "Check for updates",
                f"{APP_NAME} is up to date.",
            )
        elif manual and result.status is CheckStatus.ERROR:
            QMessageBox.warning(
                self._parent,
                "Update check failed",
                result.error_message,
            )
        self._run_pending_manual_check()

    def _worker_failed(self, message: str, *, manual: bool) -> None:
        self._checking = False
        self.checking_changed.emit(False)
        LOGGER.debug("Update worker failed: %s", message)
        if manual:
            QMessageBox.warning(
                self._parent,
                "Update check failed",
                "Could not check for updates. Check your network connection and try again.",
            )
        self._run_pending_manual_check()

    def _run_pending_manual_check(self) -> None:
        if not self._pending_manual_check:
            return
        self._pending_manual_check = False
        self._start(manual=True)

    def _show_available(self, release: ReleaseInfo) -> None:
        dialog = QMessageBox(self._parent)
        dialog.setIcon(QMessageBox.Information)
        dialog.setWindowTitle("ElectriDrive update available")
        dialog.setText(f"{APP_NAME} {release.version} is available")
        dialog.setInformativeText(
            f"You are currently using {APP_VERSION}.\n\n"
            "Updates are downloaded only when you choose to visit the official "
            "ElectriDrive GitHub release page."
        )
        download = dialog.addButton("Download update", QMessageBox.AcceptRole)
        dialog.addButton("Later", QMessageBox.RejectRole)
        dismiss = dialog.addButton(
            "Don't remind me again for this version", QMessageBox.DestructiveRole
        )
        dialog.exec()

        clicked = dialog.clickedButton()
        if clicked is download:
            # ReleaseInfo URLs are constructed from the fixed official repository;
            # release-supplied links and release text are never opened.
            QDesktopServices.openUrl(QUrl(release.page_url))
        elif clicked is dismiss:
            self._service.dismiss(release)
