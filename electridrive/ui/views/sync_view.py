from __future__ import annotations

from html import escape
from pathlib import Path
from urllib.parse import urlparse

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from electridrive.config import APP_VERSION, SyncPair
from electridrive.ui import icons
from electridrive.ui.theme import Palette
from electridrive.ui.widgets import Card, EmptyState, ProHint

_DIRS = {"two_way": "Two-way ⇄", "up_only": "Upload only ↑", "down_only": "Download only ↓"}


class SyncView(QWidget):
    def __init__(self, session, palette: Palette):
        super().__init__()
        self._session = session
        self._p = palette

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
        bar.addWidget(info, 1)
        sync_all = QPushButton("Sync all")
        sync_all.setProperty("ghost", True)
        sync_all.setIcon(icons.icon("sync", palette.text, 18))
        sync_all.setCursor(Qt.PointingHandCursor)
        sync_all.clicked.connect(lambda: self._sync_all(sync_all))
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
        while self.list_col.count():
            item = self.list_col.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        pairs = self._session.settings.sync_pairs
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
        title.setStyleSheet("font-weight: 700;")
        sub = QLabel(f"⇄  {pair.remote_folder}    ·    {_DIRS.get(pair.direction, pair.direction)}")
        sub.setProperty("role", "muted")
        col.addWidget(title)
        col.addWidget(sub)
        top.addLayout(col, 1)

        status = QLabel("Ready" if pair.enabled else "Disabled")
        status.setProperty("role", "muted")
        status.setStyleSheet("font-size: 12px;")
        top.addWidget(status)

        sync_btn = QPushButton("Sync now")
        sync_btn.setProperty("ghost", True)
        sync_btn.clicked.connect(lambda: self._sync_now(pair, status, sync_btn))
        sync_btn.setEnabled(pair.enabled)
        top.addWidget(sync_btn)

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
        return card

    def _sync_now(self, pair: SyncPair, status: QLabel, btn: QPushButton):
        # Disabled pairs must stay inert even when called programmatically.
        if not pair.enabled or not self._session.client:
            return
        btn.setEnabled(False)
        status.setText("Syncing…")
        status.setStyleSheet(f"font-size: 12px; color: {self._p.accent};")

        def work():
            from electridrive.sync.twoway import TwoWaySyncEngine
            return TwoWaySyncEngine(self._session.client, self._session.db, pair).run()

        def done(report):
            btn.setEnabled(True)
            status.setText(f"↑{report.uploaded} ↓{report.downloaded} "
                           f"⌫{report.trashed_remote + report.trashed_local} "
                           f"⚠{report.conflicts}")
            self._review_remote_path_conflicts(pair, report.remote_path_conflicts)
            if report.failed or report.unexplained_omissions:
                status.setText(f"Failed ({report.failed})")
                status.setStyleSheet(f"font-size: 12px; color: {self._p.danger};")
                QMessageBox.warning(
                    self,
                    "Sync incomplete",
                    "\n".join(report.errors) or "The completeness assertion failed.",
                )
            else:
                status.setStyleSheet(f"font-size: 12px; color: {self._p.success};")

        def fail(msg):
            btn.setEnabled(True)
            status.setText("Failed")
            status.setStyleSheet(f"font-size: 12px; color: {self._p.danger};")
            QMessageBox.warning(self, "Sync failed", msg)

        self._session.submit(work, done, fail)

    def _sync_all(self, btn: QPushButton):
        if not self._session.client:
            return
        btn.setEnabled(False)
        btn.setText("Syncing all…")

        def work():
            from electridrive.sync.runner import run_configured_pairs

            return run_configured_pairs(
                self._session.client,
                self._session.db,
                self._session.settings.sync_pairs,
            )

        def done(result):
            btn.setEnabled(True)
            btn.setText("Sync all")
            for outcome in result.outcomes:
                self._review_remote_path_conflicts(
                    outcome.pair, outcome.report.remote_path_conflicts
                )
            if result.failed_pairs:
                detail = "\n".join(result.errors[:8])
                QMessageBox.warning(
                    self,
                    "Some sync pairs failed",
                    f"Completed {result.successful_pairs} of {result.enabled_pairs} "
                    f"enabled pairs.\n\n{detail}",
                )
            else:
                QMessageBox.information(
                    self,
                    "Sync complete",
                    f"All {result.successful_pairs} enabled sync pairs completed.",
                )

        def fail(msg):
            btn.setEnabled(True)
            btn.setText("Sync all")
            QMessageBox.warning(self, "Sync all failed", msg)

        self._session.submit(work, done, fail)

    @staticmethod
    def _short_id(file_id: str) -> str:
        return f"{file_id[:4]}…{file_id[-3:]}" if len(file_id) >= 8 else f"{file_id[:2]}…"

    def _review_remote_path_conflicts(self, pair, conflicts):
        changed = False
        for conflict in conflicts:
            lines = [
                f"<b>ElectriDrive version:</b> {escape(APP_VERSION)}",
                f"<b>Remote path:</b> {escape(conflict.remote_path)}",
                f"<b>Operation mode:</b> {escape(_DIRS.get(conflict.direction, conflict.direction))}",
                f"<b>Why this was flagged / execution stopped:</b> {escape(conflict.reason)}",
                "<b>Drive candidates:</b>",
            ]
            for index, candidate in enumerate(conflict.candidates):
                kind = "Folder" if candidate.is_folder else "File"
                size = (
                    f"{candidate.size:,} bytes"
                    if candidate.size is not None else "size unavailable"
                )
                modified = candidate.modified or "modified time unavailable"
                detail = (
                    f"Candidate {index + 1} · {kind} · "
                    f"ID {self._short_id(candidate.file_id)} · "
                    f"{size} · modified {escape(modified)}"
                )
                link = candidate.web_view_link or ""
                try:
                    parsed = urlparse(link)
                    safe_preview = (
                        parsed.scheme == "https"
                        and parsed.hostname in {
                            "drive.google.com", "docs.google.com",
                        }
                        and parsed.username is None
                        and parsed.password is None
                    )
                except ValueError:
                    safe_preview = False
                if safe_preview:
                    detail += (
                        f' · <a href="{escape(link, quote=True)}">'
                        "Open Google Drive preview</a>"
                    )
                lines.append(escape(detail) if "<a " not in detail else detail)
            if conflict.resolved:
                lines.extend([
                    "",
                    "A previously reviewed canonical ID is selected. Other Drive "
                    "objects remain untouched.",
                ])
            else:
                lines.extend([
                    "",
                    "No file will be downloaded, overwritten, renamed, trashed, or "
                    "deleted while this path is unresolved.",
                ])
            box = QMessageBox(self)
            box.setWindowTitle("Drive path needs review")
            box.setTextFormat(Qt.RichText)
            box.setText("<br>".join(lines))
            for label in box.findChildren(QLabel):
                label.setOpenExternalLinks(True)
            box.setStandardButtons(QMessageBox.Ok)
            box.exec()

            if conflict.resolved:
                continue
            if (len(conflict.candidates) < 2
                    or any(candidate.is_folder for candidate in conflict.candidates)):
                continue
            choices = [
                f"Candidate {index + 1} · File ID {self._short_id(candidate.file_id)} "
                f"({candidate.size if candidate.size is not None else 'size unknown'} bytes)"
                for index, candidate in enumerate(conflict.candidates)
            ]
            choice, ok = QInputDialog.getItem(
                self,
                "Choose reviewed canonical file",
                "After previewing the candidates above, choose the Drive file ID "
                "to use for this path. The other Drive originals will remain "
                "untouched. No sync starts until you run it again.",
                choices,
                0,
                False,
            )
            if ok and choice in choices:
                candidate = conflict.candidates[choices.index(choice)]
                pair.canonical_remote_ids[conflict.remote_path] = candidate.file_id
                changed = True
        if changed:
            self._session.save()
            QMessageBox.information(
                self,
                "Canonical file saved",
                "Your reviewed Drive file ID was saved. No files were changed. "
                "Run Sync now again to apply this explicit choice.",
            )

    def _add_pair(self):
        local = QFileDialog.getExistingDirectory(self, "Choose local folder", str(Path.home()))
        if not local:
            return
        remote, ok = QInputDialog.getText(self, "Remote Drive folder",
                                          "Drive folder path:",
                                          text=self._session.settings.default_remote_folder
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
                     direction=direction, enabled=False))
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
