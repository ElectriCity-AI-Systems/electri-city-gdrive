from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from electridrive.config import APP_TAGLINE
from electridrive.ui import icons
from electridrive.ui.session import DriveSession
from electridrive.ui.theme import build_qss, get_palette
from electridrive.ui.views.explorer_view import ExplorerView
from electridrive.ui.views.login_view import LoginView
from electridrive.ui.views.settings_view import SettingsView
from electridrive.ui.views.sync_view import SyncView
from electridrive.ui.views.transfers_view import TransfersView
from electridrive.ui.views.vfs_view import VfsView
from electridrive.ui.widgets import IconButton, NavButton, StorageMeter

LOGGER = logging.getLogger(__name__)

_NAV = [
    ("section", "LIBRARY", ""),
    ("my_drive", "My Drive", "drive"),
    ("recent", "Recent", "clock"),
    ("starred", "Starred", "star"),
    ("trash", "Trash", "trash"),
    ("section", "ACTIVITY", ""),
    ("transfers", "Transfers", "transfers"),
    ("sync", "Two-way Sync", "sync"),
    ("vfs", "Virtual Drive", "server"),
]
_TITLES = {"my_drive": "My Drive", "recent": "Recent", "starred": "Starred",
           "trash": "Trash", "transfers": "Transfers", "sync": "Two-way Sync",
           "vfs": "Virtual Drive", "settings": "Settings", "search": "Search"}


class MainWindow(QMainWindow):
    def __init__(self, session: DriveSession):
        super().__init__()
        self.session = session
        self.palette_ = get_palette(session.settings.theme)
        self.views: dict[str, QWidget] = {}
        self.nav_buttons: dict[str, NavButton] = {}

        self.setWindowTitle("ElectriDrive")
        self.setMinimumSize(1040, 680)
        self.setWindowIcon(icons.icon("bolt", self.palette_.accent, 64))

        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_sidebar())
        layout.addWidget(self._build_main_area(), 1)

        # session signals
        session.connected.connect(self._on_connected)
        session.connect_failed.connect(self._on_connect_failed)
        session.log.connect(self.set_status)

        self._show_login()
        # Auto-connect silently if we already have a cached token (no browser needed).
        if session.has_token():
            self._start_connect()

    # --------------------------------------------------------------- build UI
    def _build_sidebar(self) -> QWidget:
        side = QWidget()
        side.setObjectName("Sidebar")
        side.setFixedWidth(252)
        v = QVBoxLayout(side)
        v.setContentsMargins(16, 18, 16, 16)
        v.setSpacing(6)

        brand = QHBoxLayout()
        brand.setSpacing(10)
        bolt = QLabel()
        bolt.setPixmap(icons.pixmap("bolt", self.palette_.accent, 28))
        brand.addWidget(bolt)
        bcol = QVBoxLayout()
        bcol.setSpacing(0)
        name = QLabel("ElectriDrive")
        name.setObjectName("Brand")
        sub = QLabel(APP_TAGLINE)
        sub.setObjectName("BrandSub")
        bcol.addWidget(name)
        bcol.addWidget(sub)
        brand.addLayout(bcol)
        brand.addStretch(1)
        v.addLayout(brand)
        v.addSpacing(12)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        for key, label, icon_name in _NAV:
            if key == "section":
                sec = QLabel(label)
                sec.setProperty("role", "muted")
                sec.setStyleSheet("font-size: 10px; font-weight: 800; letter-spacing: 1px;"
                                  " padding: 10px 8px 2px 8px;")
                v.addWidget(sec)
                continue
            btn = NavButton(key, label, icon_name, self.palette_)
            btn.clicked.connect(lambda _=False, k=key: self.switch_view(k))
            self.nav_group.addButton(btn)
            self.nav_buttons[key] = btn
            v.addWidget(btn)

        v.addStretch(1)
        self.storage = StorageMeter(self.palette_)
        v.addWidget(self.storage)

        settings_btn = NavButton("settings", "Settings", "settings", self.palette_)
        settings_btn.clicked.connect(lambda: self.switch_view("settings"))
        self.nav_group.addButton(settings_btn)
        self.nav_buttons["settings"] = settings_btn
        v.addWidget(settings_btn)
        return side

    def _build_main_area(self) -> QWidget:
        wrap = QWidget()
        outer = QVBoxLayout(wrap)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QWidget()
        header.setObjectName("Header")
        header.setFixedHeight(66)
        h = QHBoxLayout(header)
        h.setContentsMargins(24, 0, 18, 0)
        h.setSpacing(12)
        self.title = QLabel("ElectriDrive")
        self.title.setObjectName("ViewTitle")
        h.addWidget(self.title)
        h.addStretch(1)
        self.search = QLineEdit()
        self.search.setObjectName("Search")
        self.search.setPlaceholderText("Search Drive…")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(300)
        self.search.returnPressed.connect(self._do_search)
        h.addWidget(self.search)
        self.theme_btn = IconButton("bolt", self.palette_, "Toggle theme")
        self.theme_btn.clicked.connect(self._toggle_theme)
        h.addWidget(self.theme_btn)
        self.account_btn = IconButton("account", self.palette_, "Account / Settings")
        self.account_btn.clicked.connect(lambda: self.switch_view("settings"))
        h.addWidget(self.account_btn)
        outer.addWidget(header)

        content = QWidget()
        cv = QVBoxLayout(content)
        cv.setContentsMargins(24, 18, 24, 10)
        cv.setSpacing(0)
        self.stack = QStackedWidget()
        cv.addWidget(self.stack, 1)
        self.status_label = QLabel("")
        self.status_label.setProperty("role", "muted")
        self.status_label.setStyleSheet("font-size: 12px; padding: 6px 2px;")
        cv.addWidget(self.status_label)
        outer.addWidget(content, 1)

        # Login view is always available at stack index 0.
        self.login = LoginView(self.session, self.palette_)
        self.login.connect_clicked.connect(self._start_connect)
        self.stack.addWidget(self.login)
        return wrap

    # ----------------------------------------------------------------- connect
    def _show_login(self):
        self.login.refresh_status()
        self.stack.setCurrentWidget(self.login)
        self.title.setText("Welcome")
        self._set_nav_enabled(False)

    def _set_nav_enabled(self, enabled: bool):
        for btn in self.nav_buttons.values():
            btn.setEnabled(enabled)

    def _start_connect(self):
        self.login.set_connecting(True)
        self.set_status("Connecting to Google Drive…")
        self.session.connect_async()

    def _on_connect_failed(self, message: str):
        self.login.set_connecting(False)
        self.set_status(f"Connection failed: {message}")
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.warning(self, "Connection failed", message)

    def _on_connected(self, about):
        self._build_views()
        self._set_nav_enabled(True)
        self.storage.set_quota(about.usage, about.limit)
        self.account_btn.setToolTip(about.user_email or "Account")
        self.nav_buttons["my_drive"].setChecked(True)
        self.switch_view("my_drive")

    def _build_views(self):
        if self.views:
            return
        self.views["my_drive"] = ExplorerView(self.session, self.palette_, "my_drive")
        self.views["recent"] = ExplorerView(self.session, self.palette_, "recent")
        self.views["starred"] = ExplorerView(self.session, self.palette_, "starred")
        self.views["trash"] = ExplorerView(self.session, self.palette_, "trash")
        self.views["search"] = ExplorerView(self.session, self.palette_, "search")
        self.views["transfers"] = TransfersView(self.session, self.palette_)
        self.views["sync"] = SyncView(self.session, self.palette_)
        self.views["vfs"] = VfsView(self.session, self.palette_)
        settings = SettingsView(self.session, self.palette_)
        settings.theme_changed.connect(self._apply_theme)
        settings.reconnect_requested.connect(self._reauthenticate)
        self.views["settings"] = settings

        for key, view in self.views.items():
            self.stack.addWidget(view)
            if isinstance(view, ExplorerView):
                view.status.connect(self.set_status)
                view.open_transfers.connect(lambda: self.switch_view("transfers"))

    # ------------------------------------------------------------------- views
    def switch_view(self, key: str):
        if key not in self.views:
            return
        view = self.views[key]
        self.stack.setCurrentWidget(view)
        self.title.setText(_TITLES.get(key, "ElectriDrive"))
        if key in self.nav_buttons:
            self.nav_buttons[key].setChecked(True)
        if isinstance(view, ExplorerView) and key != "search":
            view.load()

    def _reauthenticate(self):
        """Access mode changed (token cleared) — return to login and re-consent."""
        self.set_status("Access mode changed — reconnecting…")
        self._set_nav_enabled(False)
        self._show_login()
        self._start_connect()

    def _do_search(self):
        text = self.search.text().strip()
        if not text or "search" not in self.views:
            return
        view = self.views["search"]
        view.query = text
        self.stack.setCurrentWidget(view)
        self.title.setText(f'Search')
        view.load()

    # ------------------------------------------------------------------- theme
    def _toggle_theme(self):
        new = "electric_light" if self.session.settings.theme == "electric_dark" else "electric_dark"
        self.session.settings.theme = new
        self.session.save()
        self._apply_theme(new)

    def _apply_theme(self, name: str):
        self.palette_ = get_palette(name)
        app = QApplication.instance()
        if app:
            app.setStyleSheet(build_qss(self.palette_))

    def set_status(self, message: str):
        self.status_label.setText(message)

    def closeEvent(self, event):
        self.session.shutdown()
        super().closeEvent(event)


def run_app() -> int:
    from electridrive.ui.app import run_app as _run
    return _run()
