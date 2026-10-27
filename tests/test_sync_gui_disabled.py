"""GUI guards for intentionally disabled isolated-test sync pairs (offline)."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QTextBrowser

from electridrive.ui.drive_path_review import (
    DrivePathReviewDialog, review_style, safe_preview_url,
    show_readable_warning,
)

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


def _duplicate_conflict():
    return RemotePathConflict(
        remote_path="duplicate.txt",
        direction="up_only",
        reason="Multiple Drive files share this path.",
        candidates=(
            RemotePathCandidate(
                "drive-id-candidate-001", False, 8, "2026-01-01",
                web_view_link="https://attacker.example/file",
            ),
            RemotePathCandidate(
                "drive-id-candidate-002", False, 12, "2026-01-02",
                web_view_link="https://drive.google.com/file/d/fixture-preview",
            ),
        ),
    )


def test_duplicate_dialog_explicit_palette_readable_and_no_default_choice(qapp):
    conflict = _duplicate_conflict()
    for palette_name in ("electric_dark", "electric_light"):
        palette = get_palette(palette_name)
        dialog = DrivePathReviewDialog(conflict, palette)
        try:
            style = dialog.styleSheet().lower()
            assert f"background-color: {palette.surface.lower()}" in style
            assert f"color: {palette.text.lower()}" in style
            assert "QDialog#DrivePathReview QLabel" in style
            assert "QDialog#DrivePathReview QTextBrowser" in style
            assert dialog.choice.currentIndex() == 0
            assert dialog.choice.currentData() is None
            assert not dialog.save_choice.isEnabled()
            assert dialog.selected_file_id() is None
            assert dialog.findChild(QLabel, "ReviewRemotePath").text() == (
                "Drive path: duplicate.txt"
            )
            browser = dialog.findChild(QTextBrowser, "ReviewCandidates")
            content = browser.toHtml()
            assert "Candidate 1" in content and "Candidate 2" in content
            assert "driv…001" in content and "driv…002" in content
            assert "Open Google Drive preview" in content
            assert "attacker.example" not in content
            assert "drive-id-candidate-001" not in content
        finally:
            dialog.close()
            dialog.deleteLater()
            qapp.processEvents()


def test_duplicate_dialog_requires_reviewed_selection_and_can_cancel(qapp):
    dialog = DrivePathReviewDialog(_duplicate_conflict(), get_palette("electric_dark"))
    try:
        dialog.save_choice.click()  # disabled: no implicit target
        assert dialog.selected_file_id() is None
        dialog.choice.setCurrentIndex(1)
        assert dialog.save_choice.isEnabled()
        dialog.reject()
        assert dialog.selected_file_id() is None
    finally:
        dialog.close()
        dialog.deleteLater()
        qapp.processEvents()

    dialog = DrivePathReviewDialog(_duplicate_conflict(), get_palette("electric_dark"))
    try:
        dialog.choice.setCurrentIndex(2)
        dialog.save_choice.click()
        assert dialog.selected_file_id() == "drive-id-candidate-002"
    finally:
        dialog.close()
        dialog.deleteLater()
        qapp.processEvents()


def test_gui_exposes_duplicate_candidates_and_saves_explicit_choice(
    qapp, tmp_path, monkeypatch,
):
    from PySide6.QtWidgets import QMessageBox

    pair = SyncPair(
        str(tmp_path / "source"), "ElectriDrive/Review",
        direction="up_only", enabled=True,
    )
    session = FakeSession([pair])
    saved = []
    session.save = lambda: saved.append(True)
    view = SyncView(session, get_palette("electric_dark"))
    observed = []

    def reviewed_dialog_exec(dialog):
        assert dialog.choice.currentIndex() == 0
        assert not dialog.save_choice.isEnabled()
        assert "duplicate.txt" in dialog.findChild(
            QLabel, "ReviewRemotePath"
        ).text()
        assert "Upload only" in [
            widget.text() for widget in dialog.findChildren(QLabel)
        ][1]
        dialog.choice.setCurrentIndex(2)
        dialog.save_choice.click()
        observed.append(dialog.selected_file_id())
        return dialog.result()

    monkeypatch.setattr(DrivePathReviewDialog, "exec", reviewed_dialog_exec)
    monkeypatch.setattr(QMessageBox, "information", lambda *args, **kw: None)
    try:
        view._review_remote_path_conflicts(pair, [_duplicate_conflict()])
        assert observed == ["drive-id-candidate-002"]
        assert pair.canonical_remote_ids == {
            "duplicate.txt": "drive-id-candidate-002",
        }
        assert saved == [True]
        assert session.submitted == []  # saving never uploads or downloads
        assert Settings.from_dict(Settings(sync_pairs=[pair]).to_dict()).sync_pairs[
            0
        ].canonical_remote_ids == pair.canonical_remote_ids
    finally:
        _dispose(view, qapp)


def test_duplicate_dialog_cancel_never_saves_or_starts_sync(qapp, tmp_path, monkeypatch):
    pair = SyncPair(str(tmp_path / "source"), "ElectriDrive/Review",
                    direction="up_only", enabled=True)
    session = FakeSession([pair])
    view = SyncView(session, get_palette("electric_dark"))

    def cancel_dialog(dialog):
        dialog.reject()
        return dialog.result()

    monkeypatch.setattr(DrivePathReviewDialog, "exec", cancel_dialog)
    try:
        view._review_remote_path_conflicts(pair, [_duplicate_conflict()])
        assert pair.canonical_remote_ids == {}
        assert session.submitted == []
    finally:
        _dispose(view, qapp)


@pytest.mark.parametrize("url", [
    "https://drive.google.com.evil.example/file/1",
    "https://drive.google.com@evil.example/1",
    "https://baduser@drive.google.com/1",
    "https://drive.google.com:444/file/1",
    "http://drive.google.com/file/1",
    "javascript:alert(1)",
    "https://docs.google.com.evil.example/d/1",
])
def test_google_preview_rejects_untrusted_urls(url):
    assert safe_preview_url(url) is None


def test_google_preview_allows_known_https_origin():
    assert safe_preview_url("https://drive.google.com/file/d/test") is not None


def test_warning_dialog_is_legible_against_explicit_dark_background(qapp, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    checked = []

    def inspect(box):
        assert "Sync incomplete" == box.windowTitle()
        assert box.text() == "Unresolved duplicate."
        style = box.styleSheet().lower()
        assert "background-color: #161b22" in style
        assert "color: #e6edf3" in style
        checked.append(True)
        return QMessageBox.Ok

    monkeypatch.setattr(QMessageBox, "exec", inspect)
    show_readable_warning(None, get_palette("electric_dark"),
                          "Sync incomplete", "Unresolved duplicate.")
    assert checked == [True]


def test_reviewer_contrast_exceeds_accessibility_minimum():
    def luminance(color):
        channels = [int(color[index:index+2], 16) / 255
                    for index in (1, 3, 5)]
        rgb = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
               for v in channels]
        return sum(v * k for v, k in zip(rgb, (0.2126, 0.7152, 0.0722)))

    for name in ("electric_dark", "electric_light"):
        palette = get_palette(name)
        bg, text = luminance(palette.surface), luminance(palette.text)
        ratio = (max(bg, text) + 0.05) / (min(bg, text) + 0.05)
        assert ratio >= 4.5, (name, ratio)
