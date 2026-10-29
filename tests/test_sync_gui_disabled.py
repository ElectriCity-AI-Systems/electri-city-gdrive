"""GUI guards for intentionally disabled isolated-test sync pairs (offline)."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QTextBrowser

from electridrive.ui.drive_path_review import (
    DrivePathReviewDialog, safe_preview_url,
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
        view._sync_now(pair)
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
            assert "qdialog#drivepathreview qlabel" in style
            assert "qdialog#drivepathreview qtextbrowser" in style
            assert dialog.choice.currentIndex() == 0
            assert dialog.choice.currentData() is None
            assert not dialog.save_choice.isEnabled()
            assert dialog.selected_file_id() is None
            assert dialog.findChild(QLabel, "ReviewRemotePath").text() == (
                "Drive path: duplicate.txt"
            )
            browser = dialog.findChild(QTextBrowser, "ReviewCandidates")
            content = browser.toPlainText()
            markup = browser.toHtml()
            assert "Candidate 1" in content and "Candidate 2" in content
            assert "driv…001" in content and "driv…002" in content
            assert "Open Google Drive preview" in content
            assert "drive.google.com/file/d/fixture-preview" in markup
            assert "attacker.example" not in markup
            assert "drive-id-candidate-001" not in markup
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
        assert "Upload only" in dialog.findChild(
            QLabel, "ReviewDirection"
        ).text()
        dialog.choice.setCurrentIndex(2)
        dialog.save_choice.click()
        observed.append(dialog.selected_file_id())
        return dialog.result()

    monkeypatch.setattr(DrivePathReviewDialog, "exec", reviewed_dialog_exec)
    monkeypatch.setattr(
        "electridrive.ui.views.sync_view.show_readable_information",
        lambda *args, **kw: None,
    )
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



def test_resolved_duplicate_does_not_show_dialog_again(qapp, tmp_path, monkeypatch):
    from dataclasses import replace

    pair = SyncPair(str(tmp_path / "qa"), "ElectriDrive/QA", direction="up_only",
                    enabled=True)
    session = FakeSession([pair])
    view = SyncView(session, get_palette("electric_dark"))
    conflict = replace(
        _duplicate_conflict(), resolved=True,
        canonical_id="drive-id-candidate-001",
    )

    def unexpected_dialog(_dialog):
        raise AssertionError("Resolved duplicate must not prompt again")

    monkeypatch.setattr(DrivePathReviewDialog, "exec", unexpected_dialog)
    try:
        view._review_remote_path_conflicts(pair, [conflict])
        assert session.submitted == []
        assert pair.canonical_remote_ids == {}
    finally:
        _dispose(view, qapp)


def test_successful_upload_only_sync_ignores_remote_only_duplicates(
    qapp, tmp_path, monkeypatch,
):
    from electridrive.sync.twoway import SyncReport

    pair = SyncPair(str(tmp_path / "qa"), "ElectriDrive/QA", direction="up_only",
                    enabled=True)
    session = FakeSession([pair])
    view = SyncView(session, get_palette("electric_dark"))

    def unexpected_dialog(_dialog):
        raise AssertionError("Harmless remote-only duplicates must not prompt")

    monkeypatch.setattr(DrivePathReviewDialog, "exec", unexpected_dialog)
    try:
        view._sync_now(pair)
        assert len(session.submitted) == 1
        _work, on_result, _on_error = session.submitted[-1]
        # The test drives only the GUI completion callback; no Drive API.
        on_result(SyncReport(uploaded=1, remote_path_conflicts=[
            _duplicate_conflict(),
        ]))
        assert any(label.text().startswith("↑1") for label in view.findChildren(QLabel))
        assert pair.canonical_remote_ids == {}
    finally:
        _dispose(view, qapp)

def test_stale_single_candidate_requires_explicit_review(qapp):
    conflict = RemotePathConflict(
        remote_path="same.txt", direction="up_only",
        reason="The previously selected Drive file is no longer at this path.",
        candidates=(RemotePathCandidate("survivor-id", False, 12, "2026-01-01"),),
    )
    dialog = DrivePathReviewDialog(conflict, get_palette("electric_dark"))
    try:
        assert dialog.choice.count() == 2
        assert dialog.choice.currentData() is None
        assert not dialog.save_choice.isEnabled()
        dialog.choice.setCurrentIndex(1)
        dialog.save_choice.click()
        assert dialog.selected_file_id() == "survivor-id"
    finally:
        dialog.close()
        dialog.deleteLater()
        qapp.processEvents()


def test_missing_candidate_must_be_cleared_explicitly(qapp):
    conflict = RemotePathConflict(
        remote_path="same.txt", direction="up_only",
        reason="The previously selected Drive file is no longer at this path.",
        candidates=(),
    )
    dialog = DrivePathReviewDialog(conflict, get_palette("electric_dark"))
    try:
        assert not dialog.cleared_choice()
        assert dialog.selected_file_id() is None
        dialog.clear_choice.click()
        assert dialog.cleared_choice()
        assert dialog.selected_file_id() is None
    finally:
        dialog.close()
        dialog.deleteLater()
        qapp.processEvents()


def test_gui_stale_survivor_rebind_saves_without_sync(qapp, tmp_path, monkeypatch):
    pair = SyncPair(str(tmp_path / "sync"), "ElectriDrive/QA",
                    direction="up_only", enabled=True,
                    canonical_remote_ids={"same.txt": "old-id"})
    session = FakeSession([pair])
    saved = []
    session.save = lambda: saved.append(True)
    view = SyncView(session, get_palette("electric_dark"))
    conflict = RemotePathConflict(
        remote_path="same.txt", direction="up_only",
        reason="The previously selected Drive file is no longer at this path.",
        candidates=(RemotePathCandidate("survivor-id", False, 12, "2026-01-01"),),
    )

    def select_only_survivor(dialog):
        assert dialog.choice.currentData() is None
        assert not dialog.save_choice.isEnabled()
        dialog.choice.setCurrentIndex(1)
        dialog.save_choice.click()
        return dialog.result()

    monkeypatch.setattr(DrivePathReviewDialog, "exec", select_only_survivor)
    monkeypatch.setattr(
        "electridrive.ui.views.sync_view.show_readable_information",
        lambda *args, **kwargs: None,
    )
    try:
        view._review_remote_path_conflicts(pair, [conflict])
        assert pair.canonical_remote_ids == {"same.txt": "survivor-id"}
        assert saved == [True]
        assert session.submitted == []
    finally:
        _dispose(view, qapp)


def test_gui_clear_stale_choice_saves_without_sync(qapp, tmp_path, monkeypatch):
    pair = SyncPair(str(tmp_path / "sync"), "ElectriDrive/QA",
                    direction="up_only", enabled=True,
                    canonical_remote_ids={"same.txt": "missing-id"})
    session = FakeSession([pair])
    saved = []
    session.save = lambda: saved.append(True)
    view = SyncView(session, get_palette("electric_dark"))
    conflict = RemotePathConflict(
        remote_path="same.txt", direction="up_only",
        reason="The previously selected Drive file is no longer at this path.",
        candidates=(),
    )

    def clear_missing(dialog):
        assert dialog.cleared_choice() is False
        dialog.clear_choice.click()
        return dialog.result()

    monkeypatch.setattr(DrivePathReviewDialog, "exec", clear_missing)
    monkeypatch.setattr(
        "electridrive.ui.views.sync_view.show_readable_information",
        lambda *args, **kwargs: None,
    )
    try:
        view._review_remote_path_conflicts(pair, [conflict])
        assert pair.canonical_remote_ids == {}
        assert saved == [True]
        assert session.submitted == []
    finally:
        _dispose(view, qapp)


def _blocked_report():
    from dataclasses import replace
    from electridrive.sync.twoway import SyncReport

    return SyncReport(
        failed=2, blocked_before_transfers=True,
        errors=["remote traversal incomplete: ambiguous upload target (2 checks)"],
        remote_path_conflicts=[
            _duplicate_conflict(),
            replace(_duplicate_conflict(), remote_path="second.txt"),
            replace(_duplicate_conflict(), remote_path="remote-only.txt", blocks_sync=False),
        ],
    )


def test_blocked_result_can_be_reviewed_offline_while_pair_stays_disabled(
    qapp, tmp_path, monkeypatch,
):
    pair = SyncPair(str(tmp_path / "synthetic"), "QA", direction="up_only")
    session = FakeSession([pair])
    view = SyncView(session, get_palette("electric_dark"))
    dialogs, warnings = [], []

    def cancel_review(dialog):
        dialogs.append(dialog.conflict.remote_path)
        dialog.reject()
        return dialog.result()

    monkeypatch.setattr(DrivePathReviewDialog, "exec", cancel_review)
    monkeypatch.setattr(
        "electridrive.ui.views.sync_view.show_readable_warning",
        lambda _view, _palette, title, text: warnings.append((title, text)),
    )
    try:
        next(b for b in view.findChildren(QPushButton) if b.text() == "Sync now").click()
        _work, done, _error = session.submitted[0]
        done(_blocked_report())  # no worker or API invocation
        assert dialogs == []  # diagnostic first; no unsolicited sequence of dialogs
        assert any("Blocked (2 paths)" in label.text() for label in view.findChildren(QLabel))
        assert "duplicate.txt" in warnings[0][1] and "second.txt" in warnings[0][1]
        assert "remote-only.txt" not in warnings[0][1]
        assert "No file transfers started" in warnings[0][1]
        pair.enabled = False
        session.client = None
        view._reload()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        qapp.processEvents()
        reviews = [b for b in view.findChildren(QPushButton)
                   if b.objectName() == "ReviewConflicts" and b.isEnabled()]
        assert len(reviews) == 1
        reviews[0].click()
        assert dialogs == ["duplicate.txt"]  # Cancel stops the whole review sequence
        assert pair.enabled is False and pair.canonical_remote_ids == {}
        assert len(session.submitted) == 1
        assert all(not b.isEnabled() for b in view.findChildren(QPushButton)
                   if b.text() == "Sync now")
    finally:
        _dispose(view, qapp)


@pytest.mark.parametrize("start_all", [False, True])
def test_sync_actions_do_not_submit_overlapping_jobs(qapp, tmp_path, start_all):
    pair = SyncPair(str(tmp_path / "synthetic"), "QA", direction="up_only")
    session = FakeSession([pair])
    view = SyncView(session, get_palette("electric_dark"))
    try:
        all_button = next(b for b in view.findChildren(QPushButton) if b.text() == "Sync all")
        single_button = next(b for b in view.findChildren(QPushButton) if b.text() == "Sync now")
        (all_button if start_all else single_button).click()
        assert len(session.submitted) == 1
        # Also guard programmatic entry, not just disabled buttons.
        view._sync_all(all_button)
        view._sync_now(pair)
        assert len(session.submitted) == 1
    finally:
        _dispose(view, qapp)


@pytest.mark.parametrize("disabled", [False, True])
def test_sync_completion_survives_rebuilt_pair_card(qapp, tmp_path, disabled):
    from electridrive.sync.twoway import SyncReport

    pair = SyncPair(str(tmp_path / "synthetic"), "QA", direction="up_only")
    session = FakeSession([pair])
    view = SyncView(session, get_palette("electric_dark"))
    try:
        next(b for b in view.findChildren(QPushButton) if b.text() == "Sync now").click()
        _work, done, _error = session.submitted[0]
        pair.enabled = not disabled
        view._reload()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        qapp.processEvents()
        done(SyncReport(uploaded=1))
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        qapp.processEvents()
        buttons = [b for b in view.findChildren(QPushButton) if b.text() == "Sync now"]
        assert len(buttons) == 1 and buttons[0].isEnabled() == (not disabled)
        assert any(("Disabled" in label.text()) if disabled else label.text().startswith("↑1")
                   for label in view.findChildren(QLabel))
        assert len(session.submitted) == 1
    finally:
        _dispose(view, qapp)


def test_sync_all_diagnostics_include_failure_after_eight_successes(qapp, tmp_path, monkeypatch):
    from electridrive.sync.runner import ConfiguredSyncReport, PairOutcome
    from electridrive.sync.twoway import SyncReport

    pairs = [SyncPair(str(tmp_path / f"synthetic-{index}"), f"QA-{index}", direction="up_only")
             for index in range(9)]
    session = FakeSession(pairs)
    view = SyncView(session, get_palette("electric_dark"))
    warnings = []
    monkeypatch.setattr(
        "electridrive.ui.views.sync_view.show_readable_warning",
        lambda _view, _palette, title, text: warnings.append(text),
    )
    try:
        button = next(b for b in view.findChildren(QPushButton) if b.text() == "Sync all")
        button.click()
        _work, done, _error = session.submitted[0]
        done(ConfiguredSyncReport(
            configured_pairs=9, enabled_pairs=9, successful_pairs=8, failed_pairs=1,
            outcomes=[PairOutcome(pair, SyncReport(uploaded=1)) for pair in pairs[:8]]
            + [PairOutcome(pairs[8], _blocked_report())],
        ))
        assert "QA-8" in warnings[0]
        assert "second.txt" in warnings[0]
        assert "No file transfers started" in warnings[0]
    finally:
        _dispose(view, qapp)


def test_worker_error_after_card_reload_unlocks_manual_retry(qapp, tmp_path, monkeypatch):
    pair = SyncPair(str(tmp_path / "synthetic"), "QA", direction="up_only")
    session = FakeSession([pair])
    view = SyncView(session, get_palette("electric_dark"))
    warnings = []
    monkeypatch.setattr(
        "electridrive.ui.views.sync_view.show_readable_warning",
        lambda *args: warnings.append(args[-1]),
    )
    try:
        next(b for b in view.findChildren(QPushButton) if b.text() == "Sync now").click()
        _work, _done, error = session.submitted[0]
        view._reload()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        error("synthetic task failure")
        assert warnings == ["synthetic task failure"]
        assert len(session.submitted) == 1  # no automatic retry
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        next(b for b in view.findChildren(QPushButton) if b.text() == "Sync now").click()
        assert len(session.submitted) == 2  # a separate explicit click works
    finally:
        _dispose(view, qapp)


@pytest.mark.parametrize("previously_blocked", [False, True])
def test_sync_all_worker_error_replaces_stale_status_and_candidates(
    qapp, tmp_path, monkeypatch, previously_blocked,
):
    from electridrive.sync.twoway import SyncReport

    pair = SyncPair(str(tmp_path / "synthetic"), "QA", direction="up_only")
    session = FakeSession([pair])
    view = SyncView(session, get_palette("electric_dark"))
    monkeypatch.setattr("electridrive.ui.views.sync_view.show_readable_warning", lambda *a: None)
    try:
        next(b for b in view.findChildren(QPushButton) if b.text() == "Sync now").click()
        session.submitted[0][1](_blocked_report() if previously_blocked else SyncReport(uploaded=1))
        next(b for b in view.findChildren(QPushButton) if b.text() == "Sync all").click()
        pair.enabled = False  # disabling does not cancel an existing task
        session.submitted[1][2]("synthetic aggregate failure")
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        qapp.processEvents()
        statuses = [label.text() for label in view.findChildren(QLabel)
                    if label.objectName() == "SyncStatus"]
        assert len(statuses) == 1 and "Failed (1" in statuses[0] and "Disabled" in statuses[0]
        assert all(not b.isEnabled() for b in view.findChildren(QPushButton)
                   if b.objectName() == "ReviewConflicts" or b.text() == "Sync now")
        assert not pair.enabled and len(session.submitted) == 2
    finally:
        _dispose(view, qapp)


@pytest.mark.parametrize("start_all", [False, True])
def test_submission_error_does_not_leave_sync_controls_locked(qapp, tmp_path, monkeypatch, start_all):
    pair = SyncPair(str(tmp_path / "synthetic"), "QA", direction="up_only")
    session = FakeSession([pair])
    view = SyncView(session, get_palette("electric_dark"))
    warnings = []
    monkeypatch.setattr("electridrive.ui.views.sync_view.show_readable_warning",
                        lambda *a: warnings.append(a[-1]))
    original_submit = session.submit

    def refuse(*_):
        raise RuntimeError("synthetic scheduling failure")

    session.submit = refuse
    try:
        if start_all:
            view._sync_all(next(b for b in view.findChildren(QPushButton) if b.text() == "Sync all"))
        else:
            view._sync_now(pair)
        assert warnings == ["synthetic scheduling failure"]
        assert session.submitted == []
        session.submit = original_submit
        view._sync_now(pair)
        assert len(session.submitted) == 1  # only the new explicit action is queued
    finally:
        _dispose(view, qapp)


def test_disabled_pair_cannot_be_enabled_inside_an_existing_job(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    active = SyncPair(str(tmp_path / "synthetic-active"), "QA/Active", direction="up_only")
    disabled = SyncPair(str(tmp_path / "synthetic-disabled"), "QA/Disabled", enabled=False)
    session = FakeSession([active, disabled])
    saves, warnings = [], []
    session.save = lambda: saves.append(True)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    monkeypatch.setattr("electridrive.ui.views.sync_view.show_readable_warning",
                        lambda *a: warnings.append(a[-1]))
    view = SyncView(session, get_palette("electric_dark"))
    try:
        view._sync_now(active)
        view._toggle_pair(1)
        assert not disabled.enabled
        assert not saves and warnings
        assert len(session.submitted) == 1
    finally:
        _dispose(view, qapp)


def test_reloading_cards_hides_old_controls_before_deferred_deletion(qapp, tmp_path):
    pair = SyncPair(str(tmp_path / "synthetic"), "QA", enabled=False)
    view = SyncView(FakeSession([pair]), get_palette("electric_dark"))
    try:
        view.show()
        qapp.processEvents()
        old_card = view.list_col.itemAt(0).widget()
        assert old_card.isVisible()
        view._reload()
        assert not old_card.isVisible()
    finally:
        _dispose(view, qapp)
