from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from electridrive.config import APP_TAGLINE
from electridrive.ui import icons
from electridrive.ui.theme import Palette
from electridrive.ui.widgets import Card


_FEATURES = [
    ("drive", "Your files, your call", "Grant access to files & folders via the Drive picker — privacy-first."),
    ("transfers", "Up & download", "Drag, drop, export Google Docs — with a live queue."),
    ("sync", "Two-way sync", "Keep a local folder and Drive in step, safely."),
    ("server", "Virtual Drive", "Mount your granted files on-demand — no rclone."),
]


class LoginView(QWidget):
    connect_clicked = Signal()

    def __init__(self, session, palette: Palette):
        super().__init__()
        self._session = session
        self._p = palette

        outer = QVBoxLayout(self)
        outer.setAlignment(Qt.AlignCenter)

        card = Card()
        card.setMaximumWidth(560)
        card.v.setSpacing(18)
        card.v.setContentsMargins(36, 36, 36, 36)

        # Brand
        brand_row = QHBoxLayout()
        brand_row.setSpacing(12)
        bolt = QLabel()
        bolt.setPixmap(icons.pixmap("bolt", palette.accent, 40))
        brand_row.addWidget(bolt)
        title_col = QVBoxLayout()
        title_col.setSpacing(0)
        name = QLabel("ElectriDrive")
        name.setProperty("role", "h1")
        sub = QLabel(APP_TAGLINE)
        sub.setProperty("role", "muted")
        title_col.addWidget(name)
        title_col.addWidget(sub)
        brand_row.addLayout(title_col)
        brand_row.addStretch(1)
        badge = QLabel("no rclone")
        badge.setProperty("role", "badge")
        brand_row.addWidget(badge, alignment=Qt.AlignTop)
        card.v.addLayout(brand_row)

        # Feature grid
        grid = QVBoxLayout()
        grid.setSpacing(10)
        for icon_name, t, d in _FEATURES:
            grid.addWidget(self._feature(icon_name, t, d))
        card.v.addLayout(grid)

        # Status + action
        self.status = QLabel()
        self.status.setWordWrap(True)
        card.v.addWidget(self.status)

        self.connect_btn = QPushButton("  Connect Google Drive")
        self.connect_btn.setProperty("accent", True)
        self.connect_btn.setCursor(Qt.PointingHandCursor)
        self.connect_btn.setMinimumHeight(44)
        self.connect_btn.setIcon(icons.icon("drive", palette.accent_text, 20))
        self.connect_btn.clicked.connect(self.connect_clicked)
        card.v.addWidget(self.connect_btn)

        outer.addWidget(card)
        self.refresh_status()

    def _feature(self, icon_name: str, title: str, desc: str) -> QWidget:
        row = QFrame()
        row.setObjectName("StatCard")
        lay = QHBoxLayout(row)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(12)
        glyph = QLabel()
        glyph.setPixmap(icons.pixmap(icon_name, self._p.accent, 22))
        glyph.setFixedWidth(28)
        lay.addWidget(glyph, alignment=Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(1)
        t = QLabel(title)
        t.setStyleSheet("font-weight: 700;")
        d = QLabel(desc)
        d.setProperty("role", "muted")
        d.setStyleSheet("font-size: 12px;")
        col.addWidget(t)
        col.addWidget(d)
        lay.addLayout(col, 1)
        return row

    def _ready(self) -> bool:
        from electridrive.google_api import app_client
        return self._session.has_credentials() or app_client.is_configured()

    def refresh_status(self):
        if self._ready():
            self.status.setText(
                "Click connect — your browser will open to sign in with Google."
            )
            self.status.setStyleSheet(f"color: {self._p.muted};")
            self.connect_btn.setEnabled(True)
        else:
            cfg = self._session.paths.config_dir
            self.status.setText(
                "First-time setup: add a Google OAuth Desktop client (Drive API + Picker "
                "API enabled). Set ELECTRIDRIVE_CLIENT_ID, or drop client JSON at "
                f"{cfg / 'app_client.json'} (or your own credentials.json)."
            )
            self.status.setStyleSheet(f"color: {self._p.warning};")
            self.connect_btn.setEnabled(False)

    def set_connecting(self, busy: bool):
        self.connect_btn.setEnabled(not busy and self._ready())
        self.connect_btn.setText("  Connecting…" if busy else "  Connect Google Drive")
