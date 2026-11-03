"""Session-owned sync state, shared by every view of an account."""
from __future__ import annotations

import threading
from PySide6.QtCore import QObject, Signal


class SyncState(QObject):
    changed = Signal()

    def __init__(self):
        super().__init__()
        self.busy = False
        self.active = set()
        self.reports = {}
        self.cancel = threading.Event()

    def reset(self):
        self.busy = False
        self.active.clear()
        self.reports.clear()
        self.cancel = threading.Event()
        self.changed.emit()

    def finish(self):
        self.busy = False
        self.active.clear()
        self.changed.emit()


def sync_state_for(session) -> SyncState:
    state = getattr(session, "sync_state", None)
    if state is None:
        state = session.sync_state = SyncState()
    return state
