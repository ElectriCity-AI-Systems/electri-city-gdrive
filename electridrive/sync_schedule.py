"""Sequential, cooperatively stoppable polling for unattended sync."""
from __future__ import annotations

import math
import threading


def periodic_sync(run, interval: float, stop_event: threading.Event | None = None):
    """Yield each completed report; stop on errors rather than repeating mutations."""
    validate_interval(interval)
    stop = stop_event if stop_event is not None else threading.Event()
    while not stop.is_set():
        report = run()
        yield report
        if report.failed or getattr(report, "unexplained_omissions", 0):
            return
        stop.wait(interval)


def validate_interval(interval: float) -> None:
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError('Sync interval must be a positive finite number')
