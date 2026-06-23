from __future__ import annotations

from pathlib import Path

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

from electridrive.config import SyncPair
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

        status = QLabel("Ready")
        status.setProperty("role", "muted")
        status.setStyleSheet("font-size: 12px;")
        top.addWidget(status)

        sync_btn = QPushButton("Sync now")
        sync_btn.setProperty("ghost", True)
        sync_btn.clicked.connect(lambda: self._sync_now(pair, status, sync_btn))
        top.addWidget(sync_btn)

        remove = QPushButton("Remove")
        remove.setProperty("ghost", True)
        remove.setProperty("danger", True)
        remove.clicked.connect(lambda: self._remove(index))
        top.addWidget(remove)
        card.v.addLayout(top)
        return card

    def _sync_now(self, pair: SyncPair, status: QLabel, btn: QPushButton):
        if not self._session.client:
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
            status.setStyleSheet(f"font-size: 12px; color: {self._p.success};")

        def fail(msg):
            btn.setEnabled(True)
            status.setText("Failed")
            status.setStyleSheet(f"font-size: 12px; color: {self._p.danger};")
            QMessageBox.warning(self, "Sync failed", msg)

        self._session.submit(work, done, fail)

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
        self._session.settings.sync_pairs.append(
            SyncPair(local_path=local, remote_folder=remote.strip(), direction=direction))
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
