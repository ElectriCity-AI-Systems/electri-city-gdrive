from __future__ import annotations

from datetime import datetime, timezone

from PySide6.QtCore import QSize, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from electridrive.config import PRO_COMPARE, PRO_PRICE, PRO_URL
from electridrive.ui import icons
from electridrive.ui.theme import Palette


def open_pro_url():
    QDesktopServices.openUrl(QUrl(PRO_URL))


# ------------------------------------------------------------------ formatters
def format_size(num: int | None) -> str:
    if not num:
        return "—" if num is None else "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(num)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def format_speed(bps: float) -> str:
    if bps <= 0:
        return ""
    return f"{format_size(int(bps))}/s"


def format_eta(seconds: float) -> str:
    if seconds <= 0 or seconds == float("inf"):
        return ""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


def format_relative_time(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        text = iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return iso
    delta = datetime.now(timezone.utc) - dt
    secs = delta.total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    if secs < 86400 * 30:
        return f"{int(secs // 86400)}d ago"
    return dt.strftime("%Y-%m-%d")


# --------------------------------------------------------------------- widgets
class NavButton(QPushButton):
    def __init__(self, key: str, label: str, icon_name: str, palette: Palette):
        super().__init__(label)
        self.key = key
        self.icon_name = icon_name
        self._palette = palette
        self.setCheckable(True)
        self.setProperty("nav", True)
        self.setCursor(Qt.PointingHandCursor)
        self.setIconSize(QSize(20, 20))
        self._refresh_icon(False)
        self.toggled.connect(self._refresh_icon)

    def _refresh_icon(self, checked: bool):
        color = self._palette.accent if checked else self._palette.muted
        self.setIcon(icons.icon(self.icon_name, color, 20))


class IconButton(QPushButton):
    def __init__(self, icon_name: str, palette: Palette, tooltip: str = "", size: int = 18):
        super().__init__()
        self.setProperty("iconbtn", True)
        self.setProperty("ghost", True)
        self.setCursor(Qt.PointingHandCursor)
        self.setIcon(icons.icon(icon_name, palette.text, size))
        self.setIconSize(QSize(size, size))
        if tooltip:
            self.setToolTip(tooltip)


class StorageMeter(QWidget):
    def __init__(self, palette: Palette):
        super().__init__()
        self._palette = palette
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(6)
        self.label = QLabel("Storage")
        self.label.setProperty("role", "muted")
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
        self.detail = QLabel("—")
        self.detail.setProperty("role", "muted")
        self.detail.setStyleSheet("font-size: 11px;")
        lay.addWidget(self.label)
        lay.addWidget(self.bar)
        lay.addWidget(self.detail)
        self.setObjectName("StatCard")

    def set_quota(self, usage: int, limit: int | None):
        if limit:
            self.bar.setMaximum(1000)
            self.bar.setValue(int(min(1000, usage / limit * 1000)))
            self.detail.setText(f"{format_size(usage)} of {format_size(limit)}")
        else:
            self.bar.setMaximum(0)  # indeterminate / unlimited
            self.detail.setText(f"{format_size(usage)} used")


class EmptyState(QWidget):
    def __init__(self, icon_name: str, title: str, subtitle: str, palette: Palette):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignCenter)
        lay.setSpacing(10)
        glyph = QLabel()
        glyph.setPixmap(icons.pixmap(icon_name, palette.muted, 48))
        glyph.setAlignment(Qt.AlignCenter)
        t = QLabel(title)
        t.setProperty("role", "title")
        t.setAlignment(Qt.AlignCenter)
        s = QLabel(subtitle)
        s.setProperty("role", "muted")
        s.setAlignment(Qt.AlignCenter)
        s.setWordWrap(True)
        lay.addStretch(1)
        lay.addWidget(glyph)
        lay.addWidget(t)
        lay.addWidget(s)
        lay.addStretch(1)


class Card(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.v = QVBoxLayout(self)
        self.v.setContentsMargins(18, 18, 18, 18)
        self.v.setSpacing(12)


class ProHint(QFrame):
    """A compact 'this needs full access — upgrade to Pro' banner with a buy link."""

    def __init__(self, palette: Palette, message: str):
        super().__init__()
        self.setObjectName("StatCard")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(12)
        glyph = QLabel()
        glyph.setPixmap(icons.pixmap("bolt", palette.accent, 18))
        lay.addWidget(glyph, alignment=Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(1)
        msg = QLabel(message)
        msg.setWordWrap(True)
        msg.setStyleSheet("font-weight: 600;")
        sub = QLabel(f"ElectriDrive Pro · {PRO_PRICE}  ({PRO_COMPARE})")
        sub.setProperty("role", "muted")
        sub.setStyleSheet("font-size: 12px;")
        col.addWidget(msg)
        col.addWidget(sub)
        lay.addLayout(col, 1)
        btn = QPushButton("Upgrade")
        btn.setProperty("accent", True)
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(open_pro_url)
        lay.addWidget(btn, alignment=Qt.AlignVCenter)


class TransferRow(QFrame):
    """A single live transfer line with progress, speed/ETA and a cancel button."""

    cancel_requested = Signal(int)
    retry_requested = Signal(int)

    def __init__(self, transfer_id: int, kind: str, name: str, palette: Palette):
        super().__init__()
        self.transfer_id = transfer_id
        self._palette = palette
        self.setObjectName("StatCard")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(12)

        self.glyph = QLabel()
        self.glyph.setPixmap(icons.pixmap("upload" if kind == "upload" else "download",
                                          palette.accent, 18))
        lay.addWidget(self.glyph)

        mid = QVBoxLayout()
        mid.setSpacing(4)
        self.name = QLabel(name)
        self.name.setStyleSheet("font-weight: 600;")
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)
        self.bar.setMaximum(1000)
        mid.addWidget(self.name)
        mid.addWidget(self.bar)
        wrap = QWidget()
        wrap.setLayout(mid)
        wrap.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        lay.addWidget(wrap, 1)

        self.status = QLabel("Queued")
        self.status.setProperty("role", "muted")
        self.status.setStyleSheet("font-size: 12px;")
        self.status.setMinimumWidth(150)
        self.status.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lay.addWidget(self.status)

        self.action = IconButton("close", palette, "Cancel", 16)
        self.action.clicked.connect(self._on_action)
        lay.addWidget(self.action)
        self._terminal_kind = None

    def _on_action(self):
        if self._terminal_kind == "failed":
            self.retry_requested.emit(self.transfer_id)
        else:
            self.cancel_requested.emit(self.transfer_id)

    def update_from(self, *, state: str, progress: float, status_text: str):
        self.bar.setValue(int(progress * 1000))
        self.status.setText(status_text)
        if state in ("done", "canceled", "failed"):
            self._terminal_kind = state
            color = {"done": self._palette.success, "failed": self._palette.danger,
                     "canceled": self._palette.muted}[state]
            self.status.setStyleSheet(f"font-size: 12px; color: {color};")
            self.action.setIcon(icons.icon("refresh" if state == "failed" else "check", color, 16))
            if state == "done":
                self.action.setEnabled(False)
