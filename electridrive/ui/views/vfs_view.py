from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from electridrive.ui import icons
from electridrive.ui.theme import Palette
from electridrive.ui.widgets import Card, ProHint
from electridrive.vfs import FuseMount, fuse_available


class VfsView(QWidget):
    def __init__(self, session, palette: Palette):
        super().__init__()
        self._session = session
        self._p = palette

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(14)
        root.setAlignment(Qt.AlignTop)

        intro = Card()
        head = QHBoxLayout()
        glyph = QLabel()
        glyph.setPixmap(icons.pixmap("server", palette.accent, 24))
        head.addWidget(glyph)
        t = QLabel("Virtual Drive — files on demand")
        t.setProperty("role", "title")
        head.addWidget(t)
        head.addStretch(1)
        badge = QLabel("no rclone")
        badge.setProperty("role", "badge")
        head.addWidget(badge)
        intro.v.addLayout(head)
        desc = QLabel("Mount your Drive as a folder. Files download only when you open "
                      "them, then cache locally. Backed by FUSE — no rclone, no full sync.")
        desc.setWordWrap(True)
        desc.setProperty("role", "muted")
        intro.v.addWidget(desc)
        root.addWidget(intro)

        if session.settings.scope == "file":
            root.addWidget(ProHint(
                palette, "The mount shows files you’ve granted + app-created files. To mount "
                "your entire Drive, upgrade to Pro."))

        cfg = Card()
        self._header(cfg, "settings", "Mount settings")

        mp_row = QHBoxLayout()
        mp_row.addWidget(QLabel("Mountpoint"))
        self.mp_input = QLineEdit(session.settings.mountpoint)
        mp_row.addWidget(self.mp_input, 1)
        browse = QPushButton("Browse")
        browse.setProperty("ghost", True)
        browse.clicked.connect(self._browse)
        mp_row.addWidget(browse)
        cfg.v.addLayout(mp_row)

        rf_row = QHBoxLayout()
        rf_row.addWidget(QLabel("Drive folder"))
        self.remote_input = QLineEdit("")
        self.remote_input.setPlaceholderText("(empty = whole Drive)")
        rf_row.addWidget(self.remote_input, 1)
        cfg.v.addLayout(rf_row)

        self.writable = QCheckBox("Allow writing through the mount (experimental)")
        self.writable.setChecked(session.settings.vfs_writable)
        cfg.v.addWidget(self.writable)
        root.addWidget(cfg)

        status_card = Card()
        srow = QHBoxLayout()
        self.status = QLabel()
        self.status.setStyleSheet("font-weight: 600;")
        srow.addWidget(self.status, 1)
        self.mount_btn = QPushButton("  Mount")
        self.mount_btn.setProperty("accent", True)
        self.mount_btn.setIcon(icons.icon("server", palette.accent_text, 18))
        self.mount_btn.clicked.connect(self._toggle_mount)
        srow.addWidget(self.mount_btn)
        self.open_btn = QPushButton("Open")
        self.open_btn.setProperty("ghost", True)
        self.open_btn.clicked.connect(self._open_fm)
        srow.addWidget(self.open_btn)
        status_card.v.addLayout(srow)
        root.addWidget(status_card)

        if not fuse_available():
            warn = QLabel("FUSE is not available on this system (need libfuse + fusermount).")
            warn.setStyleSheet(f"color: {palette.warning};")
            root.addWidget(warn)
            self.mount_btn.setEnabled(False)

        self._refresh()

    def _header(self, card: Card, icon_name: str, title: str):
        row = QHBoxLayout()
        glyph = QLabel()
        glyph.setPixmap(icons.pixmap(icon_name, self._p.accent, 20))
        row.addWidget(glyph)
        t = QLabel(title)
        t.setProperty("role", "title")
        row.addWidget(t)
        row.addStretch(1)
        card.v.addLayout(row)

    def _mount_obj(self) -> FuseMount:
        if self._session.fuse_mount is None:
            self._session.fuse_mount = FuseMount(
                self._session.client, self._session.paths.vfs_cache_dir)
        return self._session.fuse_mount

    def _refresh(self):
        mount = self._session.fuse_mount
        if mount and mount.is_mounted:
            self.status.setText(f"Mounted at {mount.mountpoint}")
            self.status.setStyleSheet(f"font-weight: 600; color: {self._p.success};")
            self.mount_btn.setText("  Unmount")
            self.mount_btn.setIcon(icons.icon("eject", self._p.accent_text, 18))
            self.open_btn.setEnabled(True)
        else:
            self.status.setText("Not mounted")
            self.status.setStyleSheet(f"font-weight: 600; color: {self._p.muted};")
            self.mount_btn.setText("  Mount")
            self.mount_btn.setIcon(icons.icon("server", self._p.accent_text, 18))
            self.open_btn.setEnabled(False)

    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, "Choose mountpoint", str(Path.home()))
        if path:
            self.mp_input.setText(path)

    def _toggle_mount(self):
        mount = self._mount_obj()
        if mount.is_mounted:
            mount.stop()
            self._refresh()
            return
        if not self._session.client:
            QMessageBox.warning(self, "Not connected", "Connect to Drive first.")
            return
        self._session.settings.mountpoint = self.mp_input.text().strip()
        self._session.settings.vfs_writable = self.writable.isChecked()
        self._session.save()
        try:
            mount.start(self.mp_input.text().strip(), self.remote_input.text().strip(),
                        self.writable.isChecked())
            self._refresh()
        except Exception as exc:
            QMessageBox.warning(self, "Mount failed", str(exc))

    def _open_fm(self):
        mount = self._session.fuse_mount
        if mount and mount.mountpoint:
            QDesktopServices.openUrl(QUrl.fromLocalFile(mount.mountpoint))
