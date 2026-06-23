from __future__ import annotations

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from electridrive import licensing
from electridrive.config import APP_VERSION, PRO_COMPARE, PRO_PRICE, selected_scopes
from electridrive.ui import icons
from electridrive.ui.theme import Palette
from electridrive.ui.widgets import Card, format_size, open_pro_url


class SettingsView(QWidget):
    theme_changed = Signal(str)
    reconnect_requested = Signal()

    def __init__(self, session, palette: Palette):
        super().__init__()
        self._session = session
        self._p = palette

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        body = QWidget()
        col = QVBoxLayout(body)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(14)
        col.setAlignment(Qt.AlignTop)

        col.addWidget(self._account_card())
        col.addWidget(self._appearance_card())
        col.addWidget(self._pro_card())
        col.addWidget(self._paths_card())
        col.addWidget(self._about_card())

        scroll.setWidget(body)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

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

    def _account_card(self) -> Card:
        card = Card()
        self._header(card, "account", "Account")
        about = self._session.about
        if about:
            info = QLabel(f"{about.user_name or 'Drive user'}\n{about.user_email or ''}")
            used = QLabel(f"{format_size(about.usage)} used of "
                          + (format_size(about.limit) if about.limit else "unlimited"))
        else:
            info = QLabel("Not connected")
            used = QLabel("")
        info.setStyleSheet("font-weight: 600;")
        used.setProperty("role", "muted")
        card.v.addWidget(info)
        card.v.addWidget(used)
        return card

    def _appearance_card(self) -> Card:
        card = Card()
        self._header(card, "settings", "Appearance")
        row = QHBoxLayout()
        row.addWidget(QLabel("Theme"))
        row.addStretch(1)
        combo = QComboBox()
        combo.addItem("Electric Dark", "electric_dark")
        combo.addItem("Electric Light", "electric_light")
        idx = max(0, combo.findData(self._session.settings.theme))
        combo.setCurrentIndex(idx)
        combo.currentIndexChanged.connect(
            lambda: self._set_theme(combo.currentData()))
        row.addWidget(combo)
        card.v.addLayout(row)
        hint = QLabel("Accent and surfaces update instantly; icon tints refresh on restart.")
        hint.setProperty("role", "muted")
        hint.setStyleSheet("font-size: 12px;")
        card.v.addWidget(hint)
        return card

    def _set_theme(self, name: str):
        self._session.settings.theme = name
        self._session.save()
        self.theme_changed.emit(name)

    def _pro_card(self) -> Card:
        card = Card()
        self._header(card, "bolt", "Access & ElectriDrive Pro")

        self._pro_status = QLabel()
        self._pro_status.setStyleSheet("font-weight: 700;")
        card.v.addWidget(self._pro_status)
        self._set_pro_status(licensing.verify(self._session.settings.license_key))

        free = QLabel("Free — per-file (drive.file): files the app creates + files you grant "
                      "via “Add from Drive”. No verification, ships globally.")
        pro = QLabel("Pro — full Drive: browse, two-way-sync and mount your ENTIRE Drive, "
                     "including the existing contents of any folder.")
        for lbl in (free, pro):
            lbl.setWordWrap(True)
            lbl.setProperty("role", "muted")
            card.v.addWidget(lbl)

        price_row = QHBoxLayout()
        pcol = QVBoxLayout()
        pcol.setSpacing(1)
        price = QLabel(f"ElectriDrive Pro — {PRO_PRICE}")
        price.setStyleSheet("font-weight: 800; font-size: 16px;")
        cmp = QLabel(PRO_COMPARE)
        cmp.setProperty("role", "muted")
        cmp.setStyleSheet("font-size: 12px;")
        pcol.addWidget(price)
        pcol.addWidget(cmp)
        price_row.addLayout(pcol, 1)
        donate = QPushButton("  Support & get Pro")
        donate.setProperty("accent", True)
        donate.setCursor(Qt.PointingHandCursor)
        donate.setIcon(icons.icon("bolt", self._p.accent_text, 18))
        donate.clicked.connect(open_pro_url)
        price_row.addWidget(donate)
        card.v.addLayout(price_row)

        lic_row = QHBoxLayout()
        self._lic_input = QLineEdit()
        self._lic_input.setPlaceholderText("Paste your license key…")
        activate = QPushButton("Activate")
        activate.setProperty("ghost", True)
        activate.clicked.connect(self._activate)
        lic_row.addWidget(self._lic_input, 1)
        lic_row.addWidget(activate)
        card.v.addLayout(lic_row)
        hint = QLabel("Donate (name your price) above, then paste the license key you receive.")
        hint.setProperty("role", "muted")
        hint.setStyleSheet("font-size: 12px;")
        card.v.addWidget(hint)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Access mode"))
        mode_row.addStretch(1)
        self._mode_combo = QComboBox()
        self._mode_combo.addItem("Per-file (Free)", "file")
        self._mode_combo.addItem("Full Drive (Pro)", "drive")
        self._mode_combo.setCurrentIndex(max(0, self._mode_combo.findData(self._session.settings.scope)))
        self._mode_combo.activated.connect(lambda: self._change_mode(self._mode_combo.currentData()))
        mode_row.addWidget(self._mode_combo)
        card.v.addLayout(mode_row)

        note = QLabel("Full Drive needs ElectriDrive Pro (or your own Google-verified OAuth "
                      "client). Switching signs you out so you can re-consent.")
        note.setWordWrap(True)
        note.setProperty("role", "muted")
        note.setStyleSheet("font-size: 12px;")
        card.v.addWidget(note)
        return card

    def _set_pro_status(self, lic):
        if lic:
            who = lic.name or lic.email or "licensed"
            self._pro_status.setText(f"✓ Pro active — {who}")
            self._pro_status.setStyleSheet(f"font-weight: 700; color: {self._p.success};")
        else:
            self._pro_status.setText("Free plan — Pro not active")
            self._pro_status.setStyleSheet(f"font-weight: 700; color: {self._p.muted};")

    def _is_pro(self) -> bool:
        return licensing.is_valid(self._session.settings.license_key)

    def _activate(self):
        key = self._lic_input.text().strip()
        if licensing.is_valid(key):
            self._session.settings.license_key = key
            self._session.save()
            self._set_pro_status(licensing.verify(key))
            QMessageBox.information(self, "Pro activated",
                                   "Thank you for supporting ElectriDrive! Pro is now active.")
        else:
            QMessageBox.warning(self, "Invalid key",
                                "That license key could not be verified. Check for typos, or "
                                "contact support with your donation reference.")

    def _change_mode(self, mode: str):
        if mode == self._session.settings.scope:
            return
        if mode == "drive" and not (self._is_pro() or self._session.has_credentials()):
            QMessageBox.information(
                self, "ElectriDrive Pro required",
                "Full Drive is a Pro feature. Donate (name your price) to receive a license "
                "key, then activate it here.")
            open_pro_url()
            self._mode_combo.setCurrentIndex(
                max(0, self._mode_combo.findData(self._session.settings.scope)))
            return
        self._session.set_access_mode(mode)
        self.reconnect_requested.emit()

    def _paths_card(self) -> Card:
        card = Card()
        self._header(card, "folder", "Files & folders")
        p = self._session.paths
        for label, path in (("Config", p.config_dir), ("State / database", p.state_dir),
                            ("Cache", p.cache_dir)):
            row = QHBoxLayout()
            lab = QLabel(f"{label}:  {path}")
            lab.setProperty("role", "muted")
            lab.setStyleSheet("font-size: 12px;")
            row.addWidget(lab, 1)
            btn = QPushButton("Open")
            btn.setProperty("ghost", True)
            btn.clicked.connect(lambda _=False, pp=path: QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(pp))))
            row.addWidget(btn)
            card.v.addLayout(row)
        return card

    def _about_card(self) -> Card:
        card = Card()
        self._header(card, "bolt", "About")
        text = QLabel(f"ElectriDrive {APP_VERSION} — Electric-City Drive for Linux.\n"
                      "A beautiful, safety-first Google Drive client. No rclone.")
        text.setWordWrap(True)
        text.setProperty("role", "muted")
        card.v.addWidget(text)
        return card
