from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from electridrive import qa_policy as qa
from electridrive.config import SyncPair
from electridrive.sync.twoway import SyncReport
from electridrive.ui import icons
from electridrive.ui.drive_path_review import (
    DrivePathReviewDialog, show_readable_information, show_readable_warning,
)
from electridrive.ui.theme import Palette
from electridrive.ui.widgets import Card, EmptyState, ProHint

_DIRS = {"two_way": "Two-way ⇄", "up_only": "Upload only ↑", "down_only": "Download only ↓"}


class SyncView(QWidget):
    def __init__(self, session, palette: Palette):
        super().__init__()
        self._session = session
        self._p = palette
        self._sync_busy = False
        self._active_pair_ids: set[int] = set()
        # Keep pair identity with its report: removal/recreation must not reuse
        # candidates from an unrelated pair, even if Python reuses an object ID.
        self._last_reports: dict[int, tuple[SyncPair, SyncReport]] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        if session.settings.scope == "file":
            root.addWidget(ProHint(
                palette, "Two-way sync works for ElectriDrive-managed folders. Syncing an "
                "existing Drive folder’s current contents needs full access."))

        bar = QHBoxLayout()
        info = QLabel("Keep a local folder and a Drive folder in step. "
                      "Deletions go to Trash — never lost.")
        info.setProperty("role", "muted")
        info.setWordWrap(True)
        bar.addWidget(info, 1)
        sync_all = QPushButton("Sync all")
        sync_all.setProperty("ghost", True)
        sync_all.setIcon(icons.icon("sync", palette.text, 18))
        sync_all.setCursor(Qt.PointingHandCursor)
        sync_all.clicked.connect(lambda: self._sync_all(sync_all))
        self._sync_all_button = sync_all
        bar.addWidget(sync_all)
        add = QPushButton("  Add sync pair")
        add.setProperty("accent", True)
        add.setIcon(icons.icon("plus", palette.accent_text, 18))
        add.setCursor(Qt.PointingHandCursor)
        add.clicked.connect(self._add_pair)
        bar.addWidget(add)
        root.addLayout(bar)

        self.list_col = QVBoxLayout()
        self.list_col.setSpacing(10)
        self.list_col.setAlignment(Qt.AlignTop)
        holder = QWidget()
        holder.setLayout(self.list_col)
        root.addWidget(holder, 1)

        self.empty = EmptyState("sync", "No sync pairs yet",
                                "Add a pair to mirror a folder with Drive.", palette)
        root.addWidget(self.empty, 1)
        self._reload()

    def _reload(self):
        self._sync_all_button.setEnabled(not self._sync_busy and bool(self._session.client))
        while self.list_col.count():
            item = self.list_col.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        pairs = self._session.settings.sync_pairs
        current_ids = {id(pair) for pair in pairs}
        self._last_reports = {key: value for key, value in self._last_reports.items()
                              if key in current_ids}
        self.empty.setVisible(not pairs)
        for i, pair in enumerate(pairs):
            self.list_col.addWidget(self._pair_card(i, pair))

    def _pair_card(self, index: int, pair: SyncPair) -> Card:
        card = Card()
        top = QHBoxLayout()
        glyph = QLabel()
        glyph.setPixmap(icons.pixmap("sync", self._p.accent, 22))
        top.addWidget(glyph)
        col = QVBoxLayout()
        col.setSpacing(2)
        title = QLabel(f"{pair.local_path}")
        title.setTextFormat(Qt.PlainText)
        title.setWordWrap(True)
        title.setStyleSheet("font-weight: 700;")
        sub = QLabel(f"⇄  {pair.remote_folder}    ·    {_DIRS.get(pair.direction, pair.direction)}")
        sub.setProperty("role", "muted")
        sub.setTextFormat(Qt.PlainText)
        sub.setWordWrap(True)
        col.addWidget(title)
        col.addWidget(sub)
        top.addLayout(col, 1)

        status = QLabel(self._status_text(pair))
        status.setObjectName("SyncStatus")
        status.setProperty("role", "muted")
        status.setStyleSheet("font-size: 12px;")
        report = self._report_for_pair(pair)
        if report and (report.failed or report.unexplained_omissions):
            status.setStyleSheet(f"font-size: 12px; color: {self._p.danger};")
            status.setToolTip(self._failure_message(report))
        elif id(pair) in self._active_pair_ids:
            status.setStyleSheet(f"font-size: 12px; color: {self._p.accent};")
        elif report and pair.enabled:
            status.setStyleSheet(f"font-size: 12px; color: {self._p.success};")
        top.addWidget(status)

        sync_btn = QPushButton("Sync now")
        sync_btn.setProperty("ghost", True)
        sync_btn.clicked.connect(lambda: self._sync_now(pair))
        sync_btn.setEnabled(pair.enabled and not self._sync_busy and bool(self._session.client))
        top.addWidget(sync_btn)

        review = QPushButton("Review conflicts")
        review.setObjectName("ReviewConflicts")
        review.setProperty("ghost", True)
        review.setToolTip("Review candidates from the last scan without contacting Drive. "
                          "This also works while the pair is disabled.")
        review.setEnabled(not self._sync_busy and bool(report and self._blocking_conflicts(report)))
        review.setVisible(bool(report and self._blocking_conflicts(report)))
        review.clicked.connect(lambda: self._review_cached_conflicts(pair))

        toggle = QPushButton("Disable" if pair.enabled else "Enable")
        toggle.setProperty("ghost", True)
        toggle.clicked.connect(lambda: self._toggle_pair(index))
        top.addWidget(toggle)

        remove = QPushButton("Remove")
        remove.setProperty("ghost", True)
        remove.setProperty("danger", True)
        remove.clicked.connect(lambda: self._remove(index))
        top.addWidget(remove)
        card.v.addLayout(top)
        card.v.addWidget(review, alignment=Qt.AlignLeft)
        return card

    @staticmethod
    def _blocking_conflicts(report):
        return [c for c in report.remote_path_conflicts if c.blocks_sync and not c.resolved]

    def _report_for_pair(self, pair):
        saved = self._last_reports.get(id(pair))
        return saved[1] if saved and saved[0] is pair else None

    def _status_text(self, pair):
        if id(pair) in self._active_pair_ids:
            return "Syncing…" if pair.enabled else "Disabled · Sync in progress"
        report = self._report_for_pair(pair)
        prefix = "" if pair.enabled else "Disabled · "
        if report is None:
            return "Ready" if pair.enabled else "Disabled"
        blockers = self._blocking_conflicts(report)
        if report.blocked_before_transfers:
            unit = "path" if len(blockers) == 1 else "paths"
            return prefix + (f"Blocked ({len(blockers)} {unit})" if blockers
                             else "Stopped before transfers")
        if report.failed:
            unit = "check" if report.failed == 1 else "checks"
            return prefix + f"Failed ({report.failed} {unit})"
        if report.unexplained_omissions:
            return prefix + "Incomplete"
        return prefix + (f"↑{report.uploaded} ↓{report.downloaded} "
                         f"⌫{report.trashed_remote + report.trashed_local} "
                         f"⚠{report.conflicts}")

    def _failure_message(self, report):
        lines = ["No file transfers started." if report.blocked_before_transfers
                 else "The sync did not complete."]
        blockers = self._blocking_conflicts(report)
        if blockers:
            lines.append(f"Drive paths requiring review ({len(blockers)}):")
            lines.extend(f"• {item.remote_path!r}" for item in blockers[:8])
            if len(blockers) > 8:
                lines.append(f"… and {len(blockers) - 8} more paths.")
            lines.append("Use Review conflicts to inspect the last scan, even with "
                         "this pair disabled. No Drive request or sync starts from review.")
        lines.extend(report.errors[:8])
        if not report.errors and report.unexplained_omissions:
            lines.append("The completeness assertion failed.")
        return "\n".join(lines)

    def _review_cached_conflicts(self, pair):
        report = self._report_for_pair(pair)
        if not self._sync_busy and report:
            self._review_remote_path_conflicts(pair, self._blocking_conflicts(report))

    def _finish_sync(self):
        self._sync_busy = False
        self._active_pair_ids.clear()
        self._sync_all_button.setText("Sync all")
        # Cards may have been rebuilt/disabled/removed while the worker ran.
        # Never touch the QLabel/QPushButton captured from an older card.
        self._reload()

    def _sync_now(self, pair: SyncPair):
        # Disabled pairs must stay inert even when called programmatically.
        if self._sync_busy or not pair.enabled or not self._session.client:
            return
        if not self._validate_qa(pair):
            return
        self._sync_busy = True
        self._active_pair_ids = {id(pair)}
        self._reload()

        def work():
            from electridrive.sync.twoway import TwoWaySyncEngine
            qa.validate_pair(pair)
            return TwoWaySyncEngine(self._session.client, self._session.db, pair).run()

        def done(report):
            self._last_reports[id(pair)] = (pair, report)
            self._finish_sync()
            if report.failed or report.unexplained_omissions:
                show_readable_warning(
                    self, self._p, "Sync incomplete",
                    self._failure_message(report),
                )

        def fail(msg):
            self._last_reports[id(pair)] = (pair, SyncReport(failed=1, errors=[msg]))
            self._finish_sync()
            show_readable_warning(self, self._p, "Sync failed", msg)

        try:
            self._session.submit(work, done, fail)
        except Exception as exc:
            fail(str(exc))

    def _sync_all(self, btn: QPushButton):
        if self._sync_busy or not self._session.client:
            return
        if any(not self._validate_qa(p) for p in self._session.settings.sync_pairs):
            return
        self._sync_busy = True
        self._active_pair_ids = {id(p) for p in self._session.settings.sync_pairs if p.enabled}
        btn.setText("Syncing all…")
        self._reload()

        def work():
            from electridrive.sync.runner import run_configured_pairs

            qa.validate_settings(self._session.settings)
            return run_configured_pairs(
                self._session.client,
                self._session.db,
                self._session.settings.sync_pairs,
            )

        def done(result):
            for outcome in result.outcomes:
                self._last_reports[id(outcome.pair)] = (outcome.pair, outcome.report)
            self._finish_sync()
            if result.failed_pairs:
                failed = [outcome for outcome in result.outcomes
                          if outcome.report.failed or outcome.report.unexplained_omissions]
                detail = "\n\n".join(
                    f"Drive folder: {outcome.pair.remote_folder}\n"
                    + self._failure_message(outcome.report)
                    for outcome in failed[:8]
                )
                if len(failed) > 8:
                    detail += f"\n… and {len(failed) - 8} more failed pairs."
                show_readable_warning(
                    self, self._p, "Some sync pairs failed",
                    f"Completed {result.successful_pairs} of {result.enabled_pairs} "
                    f"enabled pairs.\n\n{detail}",
                )
            else:
                show_readable_information(
                    self, self._p, "Sync complete",
                    f"All {result.successful_pairs} enabled sync pairs completed.",
                )

        def fail(msg):
            for pair in self._session.settings.sync_pairs:
                if id(pair) in self._active_pair_ids:
                    self._last_reports[id(pair)] = (pair, SyncReport(failed=1, errors=[msg]))
            self._finish_sync()
            show_readable_warning(self, self._p, "Sync all failed", msg)

        try:
            self._session.submit(work, done, fail)
        except Exception as exc:
            fail(str(exc))

    def _review_remote_path_conflicts(self, pair, conflicts):
        """One accessible dialog per conflict; nothing is selected implicitly."""
        changed = False
        for conflict in conflicts:
            # A previously reviewed canonical ID must not ask the owner
            # again on every sync attempt.
            if conflict.resolved or not conflict.blocks_sync:
                continue
            dialog = DrivePathReviewDialog(conflict, self._p, self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                break
            if dialog.cleared_choice():
                pair.canonical_remote_ids.pop(conflict.remote_path, None)
                changed = True
                continue
            candidate_id = dialog.selected_file_id()
            if candidate_id is None:
                continue
            # Persist only a reviewed Drive ID. This dialog NEVER starts sync,
            # updates, deletes, renames, or downloads any Drive objects.
            pair.canonical_remote_ids[conflict.remote_path] = candidate_id
            changed = True
        if changed:
            self._session.save()
            show_readable_information(
                self, self._p, "Drive review settings saved",
                "Your reviewed Drive association changes were saved locally. "
                "No Drive files were changed. A separate Sync now is required.",
            )

    def _validate_qa(self, pair):
        try:
            qa.validate_pair(pair)
            return True
        except qa.QaSafetyError as exc:
            show_readable_warning(self, self._p, "QA safety stop", str(exc))
            return False

    def _add_pair(self):
        policy = qa.current_policy()
        local = QFileDialog.getExistingDirectory(
            self, "Choose synthetic QA folder" if policy else "Choose local folder",
            str(policy.local_root if policy else Path.home()))
        if not local:
            return
        if policy:
            try:
                policy.local(local)
            except qa.QaSafetyError as exc:
                show_readable_warning(self, self._p, "QA safety stop", str(exc))
                return
            choices = ["qa:" + rid for rid in policy.remote_ids]
            if not choices:
                show_readable_information(self, self._p, "No registered QA cloud roots",
                    "Close this QA window first. Explicitly create a synthetic root with: "
                    "electridrive qa-create-folder --name NAME --qa-profile PROFILE. "
                    "Then restart the GUI with the same QA profile to use the registered root.")
                return
            remote, ok = QInputDialog.getItem(self, "Registered QA cloud root",
                "Choose an explicit QA root ID:", choices, 0, False)
        else:
            remote, ok = QInputDialog.getText(self, "Remote Drive folder",
                "Drive folder path:", text=self._session.settings.default_remote_folder
                + "/" + Path(local).name)
        if not ok or not remote.strip():
            return
        direction, ok = QInputDialog.getItem(
            self, "Sync direction", "Direction:",
            ["two_way", "up_only", "down_only"], 0, False)
        if not ok:
            return
        if self._overlapping_pair(local, remote.strip()):
            QMessageBox.warning(
                self, "Duplicate sync scope",
                "An existing sync pair already uses this local folder or "
                "Drive destination. Remove the duplicate or choose a separate "
                "test folder; no changes were saved.",
            )
            return
        # Safe default: adding a pair must never silently authorize a write
        # to personal Drive data, including when Sync all is clicked.
        self._session.settings.sync_pairs.append(
            SyncPair(local_path=local, remote_folder=remote.strip(),
                     direction=direction, enabled=False,
                     delete_policy="off" if policy else "trash"))
        self._session.save()
        self._reload()

    def _overlapping_pair(self, local: str, remote: str, *, exclude: int = -1) -> bool:
        canonical_local = Path(local).expanduser().resolve(strict=False)
        canonical_remote = "/".join(
            p for p in remote.replace("\\", "/").split("/") if p.strip()
        )
        for i, pair in enumerate(self._session.settings.sync_pairs):
            if i == exclude:
                continue
            if (Path(pair.local_path).expanduser().resolve(strict=False)
                    == canonical_local):
                return True
            other_remote = "/".join(
                p for p in pair.remote_folder.replace("\\", "/").split("/")
                if p.strip()
            )
            if other_remote == canonical_remote:
                return True
        return False

    def _toggle_pair(self, index: int):
        pairs = self._session.settings.sync_pairs
        if not (0 <= index < len(pairs)):
            return
        pair = pairs[index]
        if not pair.enabled:
            if not self._validate_qa(pair):
                return
            if self._sync_busy:
                show_readable_warning(
                    self, self._p, "Sync in progress",
                    "Wait for the current sync to finish before enabling another pair. "
                    "No settings were changed.",
                )
                return
            if self._overlapping_pair(
                pair.local_path, pair.remote_folder, exclude=index
            ):
                QMessageBox.warning(
                    self, "Duplicate sync scope",
                    "This pair overlaps another configured pair. Resolve the "
                    "duplicate before enabling any writes.",
                )
                return
            if QMessageBox.question(
                self, "Enable sync pair?",
                f"Local: {pair.local_path}\n"
                f"Drive: {pair.remote_folder}\n"
                f"Direction: {pair.direction}\n\n"
                "Enable this pair for future Sync now / Sync all actions? "
                "No transfer starts immediately.",
            ) != QMessageBox.Yes:
                return
            pair.enabled = True
        else:
            if QMessageBox.question(
                self, "Disable sync pair?",
                "Disable this pair for future sync actions?\n\n"
                "This does NOT cancel any transfer already in progress.",
            ) != QMessageBox.Yes:
                return
            pair.enabled = False
        self._session.save()
        self._reload()

    def _remove(self, index: int):
        pairs = self._session.settings.sync_pairs
        if 0 <= index < len(pairs):
            if QMessageBox.question(self, "Remove sync pair",
                                    "Stop syncing this pair? Files are not deleted.") == \
                    QMessageBox.Yes:
                pairs.pop(index)
                self._session.save()
                self._reload()
