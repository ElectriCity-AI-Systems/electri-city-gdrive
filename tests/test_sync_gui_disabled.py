"""GUI guards for intentionally disabled isolated-test sync pairs (offline)."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from electridrive.config import Settings, SyncPair
from electridrive.sync.twoway import RemotePathCandidate, RemotePathConflict
from electridrive.ui.theme import get_palette
from electridrive.ui.views.sync_view import SyncView


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


def test_gui_exposes_duplicate_candidates_and_saves_explicit_choice(
    qapp, tmp_path, monkeypatch,
):
    from PySide6.QtWidgets import QMessageBox, QInputDialog

    pair = SyncPair(
        str(tmp_path / "source"), "ElectriDrive/Review",
        direction="up_only", enabled=True,
    )
    session = FakeSession([pair])
    saved = []
    session.save = lambda: saved.append(True)
    view = SyncView(session, get_palette("electric_dark"))
    messages = []
    monkeypatch.setattr(
        QMessageBox, "exec", lambda box: messages.append(box.text()) or QMessageBox.Ok,
    )
    monkeypatch.setattr(
        QMessageBox, "information", lambda *args, **kwargs: messages.append(args[-1]),
    )
    monkeypatch.setattr(
        QInputDialog, "getItem",
        lambda *args, **kwargs: (args[3][1], True),
    )
    conflict = RemotePathConflict(
        remote_path="duplicate.txt",
        direction="up_only",
        reason="Multiple Drive files share this path.",
        candidates=(
            RemotePathCandidate(
                "drive-id-candidate-001", False, 8, "2026-01-01",
                web_view_link="https://attacker.example/file",
            ),
            RemotePathCandidate("drive-id-candidate-002", False, 12, "2026-01-02"),
        ),
    )
    try:
        view._review_remote_path_conflicts(pair, [conflict])
        display = "\n".join(messages)
        assert "duplicate.txt" in display
        assert "ElectriDrive version:" in display
        assert "Upload only" in display
        assert "Multiple Drive files share this path." in display
        assert "drive-id-candidate-001" not in display
        assert "drive-id-candidate-002" not in display
        assert "driv…001" in display
        assert "driv…002" in display
        assert "Open Google Drive preview" not in display
        assert pair.canonical_remote_ids == {
            "duplicate.txt": "drive-id-candidate-002",
        }
        assert saved == [True]
        assert Settings.from_dict(Settings(sync_pairs=[pair]).to_dict()).sync_pairs[
            0
        ].canonical_remote_ids == pair.canonical_remote_ids
    finally:
        _dispose(view, qapp)
