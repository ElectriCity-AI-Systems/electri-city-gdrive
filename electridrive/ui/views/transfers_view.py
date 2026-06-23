from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from electridrive.ui.theme import Palette
from electridrive.ui.widgets import (
    EmptyState,
    TransferRow,
    format_eta,
    format_size,
    format_speed,
)


class TransfersView(QWidget):
    def __init__(self, session, palette: Palette):
        super().__init__()
        self._session = session
        self._p = palette
        self._rows: dict[int, TransferRow] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        bar = QHBoxLayout()
        self.summary = QLabel("No transfers yet")
        self.summary.setProperty("role", "muted")
        bar.addWidget(self.summary, 1)
        self.clear_btn = QPushButton("Clear finished")
        self.clear_btn.setProperty("ghost", True)
        self.clear_btn.clicked.connect(self._clear_finished)
        bar.addWidget(self.clear_btn)
        root.addLayout(bar)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.NoFrame)
        self.container = QWidget()
        self.col = QVBoxLayout(self.container)
        self.col.setContentsMargins(0, 0, 0, 0)
        self.col.setSpacing(8)
        self.col.addStretch(1)
        self.scroll.setWidget(self.container)

        self.empty = EmptyState("transfers", "No transfers",
                                "Uploads and downloads will appear here.", palette)
        root.addWidget(self.empty, 1)
        root.addWidget(self.scroll, 1)
        self.scroll.hide()

        session.transfer_changed.connect(self.on_transfer)

    def on_transfer(self, t):
        row = self._rows.get(t.id)
        if row is None:
            row = TransferRow(t.id, t.kind.value, t.name, self._p)
            row.cancel_requested.connect(self._cancel)
            row.retry_requested.connect(self._retry)
            self._rows[t.id] = row
            self.col.insertWidget(0, row)
            self.empty.hide()
            self.scroll.show()

        row.update_from(state=t.state.value, progress=t.progress,
                        status_text=self._status_text(t))
        self._update_summary()

    def _status_text(self, t) -> str:
        state = t.state.value
        if state == "running":
            speed = t.speed_bps()
            parts = [f"{int(t.progress * 100)}%"]
            if speed > 0:
                parts.append(format_speed(speed))
                remaining = (t.total - t.bytes_done) / speed if speed else 0
                eta = format_eta(remaining)
                if eta:
                    parts.append(eta)
            return " · ".join(parts)
        if state == "queued":
            return "Queued"
        if state == "done":
            return f"Done · {format_size(t.total or t.bytes_done)}"
        if state == "failed":
            return "Failed · retry"
        if state == "canceled":
            return "Canceled"
        return state

    def _update_summary(self):
        active = sum(1 for r in self._rows if not self._is_terminal(r))
        total = len(self._rows)
        if active:
            self.summary.setText(f"{active} active · {total} total")
        else:
            self.summary.setText(f"{total} transfer(s) · all finished")

    def _is_terminal(self, transfer_id: int) -> bool:
        if not self._session.transfers:
            return True
        for t in self._session.transfers.snapshot():
            if t.id == transfer_id:
                return t.is_terminal
        return True

    def _cancel(self, transfer_id: int):
        if self._session.transfers:
            self._session.transfers.cancel(transfer_id)

    def _retry(self, transfer_id: int):
        if self._session.transfers:
            self._session.transfers.retry(transfer_id)

    def _clear_finished(self):
        for tid in list(self._rows):
            if self._is_terminal(tid):
                row = self._rows.pop(tid)
                row.setParent(None)
                row.deleteLater()
        if not self._rows:
            self.scroll.hide()
            self.empty.show()
        self._update_summary()
