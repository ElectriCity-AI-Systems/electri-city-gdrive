from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QApplication

from electridrive.logging_setup import configure_logging
from electridrive.ui import icons
from electridrive.ui.session import DriveSession
from electridrive.ui.theme import build_qss, get_palette

_ICON_PATH = Path(__file__).resolve().parents[2] / "assets" / "electridrive.png"


def create_app(argv=None) -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("ElectriDrive")
    app.setApplicationDisplayName("ElectriDrive")
    app.setDesktopFileName("electridrive")
    app.setWindowIcon(QIcon(str(_ICON_PATH)) if _ICON_PATH.exists()
                      else icons.icon("bolt", "#22D3EE", 64))
    font = QFont()
    font.setFamilies(["Inter", "Ubuntu", "Cantarell", "Noto Sans", "sans-serif"])
    font.setPointSize(10)
    app.setFont(font)
    return app


def run_app() -> int:
    configure_logging()
    app = create_app()
    session = DriveSession()
    app.setStyleSheet(build_qss(get_palette(session.settings.theme)))

    from electridrive.ui.main_window import MainWindow

    window = MainWindow(session)
    window.show()
    return app.exec()
