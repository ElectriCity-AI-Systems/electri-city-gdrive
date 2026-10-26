"""GUI guards for intentionally disabled isolated-test sync pairs (offline)."""
from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from electridrive.config import Settings, SyncPair
from electridrive.ui.theme import get_palette
from electridrive.ui.views.sync_view import SyncView


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


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
        view.close()


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
        view.close()


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
        view.close()


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
        view.close()


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
        view.close()
