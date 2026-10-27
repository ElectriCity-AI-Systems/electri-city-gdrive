"""Accessible, explicit owner review of ambiguous Google Drive paths.

No Drive API operations occur in this dialog. Choosing an ID only persists a
canonical association for a later, separately triggered sync run.
"""
from __future__ import annotations

from html import escape
from urllib.parse import urlparse

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QMessageBox,
    QTextBrowser,
    QVBoxLayout,
)

from electridrive.config import APP_VERSION
from electridrive.ui.theme import Palette


_DIRECTIONS = {
    "two_way": "Two-way sync",
    "up_only": "Upload only",
    "down_only": "Download only",
}


def safe_preview_url(link: str | None) -> str | None:
    """Only allow direct, known Google preview origins (no credentials or redirects)."""
    if not link:
        return None
    try:
        url = urlparse(link)
        if (url.scheme != "https"
                or url.hostname not in {"drive.google.com", "docs.google.com"}
                or url.username is not None or url.password is not None
                or url.port not in {None, 443}):
            return None
        return link
    except ValueError:
        return None


def short_drive_id(value: str) -> str:
    if len(value) >= 8:
        return f"{value[:4]}…{value[-3:]}"
    return f"{value[:2]}…"


def review_style(palette: Palette) -> str:
    """Native dialogs can otherwise inherit white OS backgrounds + dark text QSS."""
    return f"""
        QDialog#DrivePathReview {{
            background-color: {palette.surface};
            color: {palette.text};
        }}
        QDialog#DrivePathReview QLabel {{
            background: transparent;
            color: {palette.text};
            font-size: 13px;
        }}
        QDialog#DrivePathReview QTextBrowser {{
            background-color: {palette.surface2};
            color: {palette.text};
            border: 1px solid {palette.border_strong};
            padding: 10px;
            selection-background-color: {palette.accent};
            selection-color: {palette.accent_text};
        }}
        QDialog#DrivePathReview QComboBox {{
            background-color: {palette.surface2};
            color: {palette.text};
            border: 1px solid {palette.border_strong};
            padding: 8px;
            min-height: 26px;
        }}
        QDialog#DrivePathReview QComboBox QAbstractItemView {{
            background-color: {palette.surface2};
            color: {palette.text};
            selection-background-color: {palette.selection};
            selection-color: {palette.text};
        }}
        QDialog#DrivePathReview QPushButton {{
            background-color: {palette.surface2};
            color: {palette.text};
            border: 1px solid {palette.border_strong};
            border-radius: 7px;
            padding: 9px;
            min-width: 105px;
        }}
        QDialog#DrivePathReview QPushButton:disabled {{
            background-color: {palette.surface};
            color: {palette.muted};
        }}
        QDialog#DrivePathReview QPushButton#SaveChoice:enabled {{
            background-color: {palette.accent};
            color: {palette.accent_text};
            border: 1px solid {palette.accent};
            font-weight: bold;
        }}
    """


class DrivePathReviewDialog(QDialog):
    """Readable on dark/light desktops; no destructive actions or implicit selection."""

    def __init__(self, conflict, palette: Palette, parent=None):
        super().__init__(parent)
        self.conflict = conflict
        self.setObjectName("DrivePathReview")
        self.setWindowTitle("Review duplicate Google Drive files — ElectriDrive")
        self.resize(720, 590)
        self.setMinimumSize(540, 380)
        self.setStyleSheet(review_style(palette))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)

        headline = QLabel("Several Google Drive files have the same path.")
        headline.setObjectName("ReviewHeadline")
        headline.setWordWrap(True)
        headline.setStyleSheet(f"font-size: 17px; font-weight: 700; color: {palette.text};")
        layout.addWidget(headline)

        source = QLabel(
            f"ElectriDrive {APP_VERSION}  ·  {_DIRECTIONS.get(conflict.direction, conflict.direction)}"
        )
        source.setObjectName("ReviewDirection")
        source.setWordWrap(True)
        layout.addWidget(source)

        path_label = QLabel()
        path_label.setObjectName("ReviewRemotePath")
        path_label.setTextFormat(Qt.PlainText)
        path_label.setWordWrap(True)
        path_label.setText("Drive path: " + conflict.remote_path)
        layout.addWidget(path_label)

        explanation = QLabel(
            "Sync was stopped for safety: " + conflict.reason
        )
        explanation.setObjectName("ReviewExplanation")
        explanation.setTextFormat(Qt.PlainText)
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        self.candidates = QTextBrowser(self)
        self.candidates.setObjectName("ReviewCandidates")
        self.candidates.setOpenExternalLinks(True)
        self.candidates.setReadOnly(True)
        self.candidates.setHtml(self._render_candidates(palette))
        layout.addWidget(self.candidates, 1)

        self.choice = QComboBox(self)
        self.choice.setObjectName("ReviewCanonicalChoice")
        selectable = (
            not conflict.resolved
            and len(conflict.candidates) >= 2
            and all(not candidate.is_folder for candidate in conflict.candidates)
        )
        if selectable:
            info = QLabel(
                "After inspecting the previews, select the exact Drive file to "
                "associate with this path. Other originals remain untouched."
            )
            info.setWordWrap(True)
            layout.addWidget(info)
            self.choice.addItem("— No file selected (safe default) —", userData=None)
            for index, candidate in enumerate(conflict.candidates, start=1):
                size = f"{candidate.size:,} B" if candidate.size is not None else "unknown size"
                self.choice.addItem(
                    f"Candidate {index}: {short_drive_id(candidate.file_id)} · {size}",
                    userData=candidate.file_id,
                )
            layout.addWidget(self.choice)
        else:
            self.choice.setVisible(False)

        warning = QLabel(
            "Saving a choice makes NO changes in Drive. A separately started "
            "Sync now may update the chosen Drive file. Cancel to keep everything unchanged."
            if selectable else
            "No automatic selection or file changes will be made. "
            "Close to leave this conflict unresolved."
        )
        warning.setObjectName("ReviewSafetyWarning")
        warning.setTextFormat(Qt.PlainText)
        warning.setWordWrap(True)
        layout.addWidget(warning)

        actions = QHBoxLayout()
        actions.addStretch()
        cancel = QPushButton("Close without changes", self)
        cancel.setObjectName("CancelReview")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)

        self.save_choice = QPushButton("Save reviewed choice", self)
        self.save_choice.setObjectName("SaveChoice")
        self.save_choice.setEnabled(False)
        if selectable:
            self.choice.currentIndexChanged.connect(
                lambda _: self.save_choice.setEnabled(self.choice.currentData() is not None)
            )
            self.save_choice.clicked.connect(self._confirm_selection)
            actions.addWidget(self.save_choice)
        else:
            self.save_choice.setVisible(False)
        layout.addLayout(actions)

    def _render_candidates(self, palette: Palette) -> str:
        elements = [
            f'<html><body style="color:{palette.text}; background-color:{palette.surface2}; '
            'font-family:sans-serif; font-size:13px">',
            '<p style="font-weight:bold;">Google Drive candidates</p>',
        ]
        for index, item in enumerate(self.conflict.candidates, start=1):
            kind = "Folder" if item.is_folder else "File"
            size = f"{item.size:,} bytes" if item.size is not None else "size unavailable"
            timestamp = escape(item.modified or "modified time unavailable")
            parts = [
                f"<b>Candidate {index}</b> · {kind} · "
                f"ID {escape(short_drive_id(item.file_id))}<br>"
                f"Size: {escape(size)} · Last changed: {timestamp}"
            ]
            preview = safe_preview_url(item.web_view_link)
            if preview:
                parts.append(
                    f'<br><a style="color:{palette.accent};" '
                    f'href="{escape(preview, quote=True)}">'
                    "Open Google Drive preview</a>"
                )
            elements.append(
                f'<p style="border-bottom:1px solid {palette.border_strong}; '
                f'padding:8px;">{"".join(parts)}</p>'
            )
        elements.append("</body></html>")
        return "".join(elements)

    def _confirm_selection(self):
        if self.choice.currentData() is not None:
            self.accept()

    def selected_file_id(self) -> str | None:
        if self.result() != QDialog.Accepted:
            return None
        value = self.choice.currentData()
        allowed = {candidate.file_id for candidate in self.conflict.candidates}
        return value if value in allowed else None


def _readable_message(parent, palette: Palette, title: str,
                      message: str, icon) -> None:
    """Avoid pale native QMessageBox surfaces inheriting near-white app text."""
    box = QMessageBox(parent)
    box.setObjectName("SafeSyncMessage")
    box.setWindowTitle(title)
    box.setIcon(icon)
    box.setTextFormat(Qt.PlainText)
    box.setText(message)
    box.setStandardButtons(QMessageBox.Ok)
    box.setStyleSheet(f"""
        QMessageBox#SafeSyncMessage {{
            background-color: {palette.surface};
            color: {palette.text};
        }}
        QMessageBox#SafeSyncMessage QLabel {{
            background: transparent;
            color: {palette.text};
            font-size: 13px;
        }}
        QMessageBox#SafeSyncMessage QPushButton {{
            background-color: {palette.surface2};
            color: {palette.text};
            border: 1px solid {palette.border_strong};
            border-radius: 7px;
            min-width: 80px;
            padding: 8px;
        }}
    """)
    box.exec()


def show_readable_warning(parent, palette: Palette, title: str, message: str) -> None:
    _readable_message(parent, palette, title, message, QMessageBox.Warning)


def show_readable_information(parent, palette: Palette, title: str,
                              message: str) -> None:
    _readable_message(parent, palette, title, message, QMessageBox.Information)
