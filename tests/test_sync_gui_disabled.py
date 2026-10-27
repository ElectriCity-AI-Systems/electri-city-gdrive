"""GUI guards for intentionally disabled isolated-test sync pairs (offline)."""
from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
)

from electridrive.config import Settings, SyncPair
from electridrive.ui.session import DriveSession
from electridrive.ui.theme import get_palette
from electridrive.ui.views.sync_view import SyncView
from fakes import FakeDrive


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _dispose(view, qapp):
    """Flush Qt DeferredDelete after the GUI dynamically rebuilds cards.

    Closing a top-level widget alone leaves pending QObject destruction
    until interpreter teardown, which can crash PySide's QApplication.
    """
    view.close()
    view.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    qapp.processEvents()


class FakeSession:
    def __init__(self, pairs):
        self.settings = Settings(scope="file", sync_pairs=pairs)
        self.client = object()
        self.submitted = []

    def submit(self, *args):
        self.submitted.append(args)

    def save(self):
        raise AssertionError("GUI read-only smoke must not alter settings")


def test_disabled_pair_card_cannot_start_sync_even_with_client(qapp, tmp_path):
    pair = SyncPair(
        local_path=str(tmp_path / "Musik"),
        remote_folder="ElectriDrive/Musik",
        direction="up_only",
        enabled=False,
    )
    session = FakeSession([pair])
    view = SyncView(session, get_palette("electric_dark"))
    try:
        buttons = [b for b in view.findChildren(QPushButton) if b.text() == "Sync now"]
        assert len(buttons) == 1
        assert not buttons[0].isEnabled()
        assert any(x.text() == "Disabled" for x in view.findChildren(QLabel))
        view._sync_now(pair, QLabel(), QPushButton())
        assert session.submitted == []
    finally:
        _dispose(view, qapp)


def test_enabled_pair_still_has_actionable_sync_button(qapp, tmp_path):
    pair = SyncPair(
        local_path=str(tmp_path / "Musik"),
        remote_folder="ElectriDrive/Musik",
        direction="up_only",
        enabled=True,
    )
    session = FakeSession([pair])
    view = SyncView(session, get_palette("electric_dark"))
    try:
        buttons = [b for b in view.findChildren(QPushButton) if b.text() == "Sync now"]
        assert len(buttons) == 1
        assert buttons[0].isEnabled()
        assert any(x.text() == "Ready" for x in view.findChildren(QLabel))
        # No click or API call; existing enabled state remains visible.
        assert session.submitted == []
    finally:
        _dispose(view, qapp)


def test_gui_new_pair_starts_disabled_and_requires_explicit_enable(
    qapp, tmp_path, monkeypatch,
):
    from PySide6.QtWidgets import QFileDialog, QInputDialog, QMessageBox

    folder = tmp_path / "harmless-test"
    folder.mkdir()
    session = FakeSession([])
    saved = []
    session.save = lambda: saved.append(True)
    view = SyncView(session, get_palette("electric_dark"))
    monkeypatch.setattr(
        QFileDialog, "getExistingDirectory", lambda *a, **k: str(folder),
    )
    monkeypatch.setattr(
        QInputDialog, "getText",
        lambda *a, **k: ("ElectriDrive/Safe-Test-Only", True),
    )
    monkeypatch.setattr(
        QInputDialog, "getItem", lambda *a, **k: ("up_only", True),
    )
    try:
        view._add_pair()
        assert saved == [True]
        assert len(session.settings.sync_pairs) == 1
        assert session.settings.sync_pairs[0].enabled is False
        assert session.submitted == []
        assert any(x.text() == "Disabled" for x in view.findChildren(QLabel))
        monkeypatch.setattr(
            QMessageBox, "question", lambda *a, **k: QMessageBox.No,
        )
        view._toggle_pair(0)
        assert session.settings.sync_pairs[0].enabled is False
        monkeypatch.setattr(
            QMessageBox, "question", lambda *a, **k: QMessageBox.Yes,
        )
        view._toggle_pair(0)
        assert session.settings.sync_pairs[0].enabled is True
        assert len(saved) == 2
        assert session.submitted == []  # enabling does not start a sync
        view._toggle_pair(0)
        assert session.settings.sync_pairs[0].enabled is False
        assert len(saved) == 3
    finally:
        _dispose(view, qapp)


def test_gui_rejects_duplicate_local_scope_even_with_disabled_pair(
    qapp, tmp_path, monkeypatch,
):
    from PySide6.QtWidgets import QFileDialog, QInputDialog, QMessageBox

    folder = tmp_path / "Dokumente"
    folder.mkdir()
    pair = SyncPair(str(folder), "ElectriDrive/Dokumente",
                    direction="up_only", enabled=False)
    session = FakeSession([pair])
    warnings = []
    monkeypatch.setattr(
        QMessageBox, "warning",
        lambda *a, **k: warnings.append(True),
    )
    monkeypatch.setattr(
        QFileDialog, "getExistingDirectory", lambda *a, **k: str(folder),
    )
    monkeypatch.setattr(
        QInputDialog, "getText",
        lambda *a, **k: ("ElectriDrive/Dokumente-Duplicate", True),
    )
    monkeypatch.setattr(
        QInputDialog, "getItem", lambda *a, **k: ("up_only", True),
    )
    view = SyncView(session, get_palette("electric_dark"))
    try:
        view._add_pair()
        assert warnings == [True]
        assert len(session.settings.sync_pairs) == 1
        assert session.submitted == []
    finally:
        _dispose(view, qapp)


def test_gui_refuses_enable_when_two_disabled_pairs_share_drive_destination(
    qapp, tmp_path, monkeypatch,
):
    from PySide6.QtWidgets import QMessageBox

    a = SyncPair(str(tmp_path / "A"), "ElectriDrive/Shared",
                 direction="up_only", enabled=False)
    b = SyncPair(str(tmp_path / "B"), "ElectriDrive/Shared",
                 direction="up_only", enabled=False)
    session = FakeSession([a, b])
    warnings = []
    monkeypatch.setattr(
        QMessageBox, "warning", lambda *a, **k: warnings.append(True),
    )
    view = SyncView(session, get_palette("electric_dark"))
    try:
        view._toggle_pair(0)
        assert warnings == [True]
        assert not session.settings.sync_pairs[0].enabled
        assert not session.settings.sync_pairs[1].enabled
    finally:
        _dispose(view, qapp)


def test_fresh_profile_sync_actions_only_touch_explicitly_enabled_pair(
    qapp, tmp_path, monkeypatch,
):
    for key in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(key, str(tmp_path / key))

    session = DriveSession()
    view = None
    try:
        assert session.settings.sync_pairs == []
        assert not session.has_credentials()
        assert not session.has_token()
        assert session.client is None

        first_root = tmp_path / "First selected tree"
        second_root = tmp_path / "Second selected tree"
        first_root.mkdir()
        second_root.mkdir()
        (first_root / "first-only.txt").write_text("first", encoding="utf-8")
        (second_root / "must-stay-disabled.txt").write_text(
            "disabled", encoding="utf-8"
        )

        selected_folders = iter((str(first_root), str(second_root)))
        selected_remote_paths = iter((
            "ElectriDrive/GUI-Isolated-First",
            "ElectriDrive/GUI-Isolated-Disabled",
        ))
        monkeypatch.setattr(
            QFileDialog, "getExistingDirectory",
            lambda *a, **k: next(selected_folders),
        )
        monkeypatch.setattr(
            QInputDialog, "getText",
            lambda *a, **k: (next(selected_remote_paths), True),
        )
        monkeypatch.setattr(
            QInputDialog, "getItem", lambda *a, **k: ("up_only", True),
        )
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)

        view = SyncView(session, get_palette("electric_dark"))
        view._add_pair()
        view._add_pair()
        assert all(not pair.enabled for pair in session.settings.sync_pairs)
        assert not session.has_credentials()
        assert not session.has_token()

        fake_drive = FakeDrive()
        session.client = fake_drive
        submitted = []
        session.submit = lambda work, on_result=None, on_error=None: submitted.append(
            (work, on_result, on_error)
        )

        sync_all = next(
            button for button in view.findChildren(QPushButton)
            if button.text() == "Sync all"
        )
        sync_all.click()
        work, done, _fail = submitted.pop()
        disabled_result = work()
        done(disabled_result)
        assert disabled_result.enabled_pairs == 0
        assert disabled_result.successful_pairs == 0
        assert fake_drive.uploads == []
        assert fake_drive.updates == []
        assert len(fake_drive.nodes) == 1

        monkeypatch.setattr(
            QMessageBox, "question", lambda *a, **k: QMessageBox.Yes,
        )
        view._toggle_pair(0)
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        qapp.processEvents()

        sync_buttons = [
            button for button in view.findChildren(QPushButton)
            if button.text() == "Sync now"
        ]
        assert len(sync_buttons) == 2
        assert sync_buttons[0].isEnabled()
        assert not sync_buttons[1].isEnabled()
        sync_buttons[0].click()
        work, done, _fail = submitted.pop()
        first_result = work()
        done(first_result)
        assert first_result.failed == 0
        assert [Path(item[0]).name for item in fake_drive.uploads] == [
            "first-only.txt"
        ]
        assert not any(
            node["name"] == "must-stay-disabled.txt"
            for node in fake_drive.nodes.values()
        )

        sync_all.click()
        work, done, _fail = submitted.pop()
        enabled_result = work()
        done(enabled_result)
        assert enabled_result.enabled_pairs == 1
        assert enabled_result.successful_pairs == 1
        assert enabled_result.failed_pairs == 0
        assert len(fake_drive.uploads) == 1
        assert fake_drive.updates == []
        assert all(not node["trashed"] for node in fake_drive.nodes.values())
        assert not any(
            node["name"] == "GUI-Isolated-Disabled"
            for node in fake_drive.nodes.values()
        )
    finally:
        if view is not None:
            _dispose(view, qapp)
        session.shutdown()
