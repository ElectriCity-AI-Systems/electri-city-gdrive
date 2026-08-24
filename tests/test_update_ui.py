from __future__ import annotations

import json
import os
from types import SimpleNamespace
from urllib.error import URLError

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

from electridrive.ui.update_controller import UpdateController
from electridrive.updater.update_checker import (
    GitHubReleaseSource,
    UpdateChecker,
    UpdateCheckService,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class _ImmediateSession:
    def __init__(self):
        self.settings = SimpleNamespace(
            last_update_check="", dismissed_update_version=""
        )
        self.save_count = 0

    def save(self):
        self.save_count += 1

    def submit(self, fn, on_result=None, on_error=None, *args, **kwargs):
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            if on_error:
                on_error(str(exc))
        else:
            if on_result:
                on_result(result)


def _controller(qapp, response):
    session = _ImmediateSession()
    parent = QWidget()
    controller = UpdateController(session, parent)

    def transport(_request, _timeout):
        if isinstance(response, Exception):
            raise response
        return json.dumps(response).encode("utf-8")

    controller._service = UpdateCheckService(
        session.settings,
        session.save,
        current_version="2.1.0",
        checker=UpdateChecker(GitHubReleaseSource(transport=transport)),
    )
    return controller, parent


def test_automatic_network_error_is_silent(monkeypatch, qapp):
    controller, parent = _controller(qapp, URLError("offline"))
    dialogs = []
    monkeypatch.setattr(
        QMessageBox, "warning", lambda *args, **kwargs: dialogs.append("warning")
    )
    monkeypatch.setattr(
        QMessageBox, "information", lambda *args, **kwargs: dialogs.append("info")
    )

    controller.start_automatic_check()

    assert dialogs == []
    parent.close()


def test_manual_check_reports_up_to_date(monkeypatch, qapp):
    response = [
        {"tag_name": "v2.1.0", "draft": False, "prerelease": False}
    ]
    controller, parent = _controller(qapp, response)
    dialogs = []
    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda _parent, title, message: dialogs.append((title, message)),
    )

    controller.check_manually()

    assert dialogs == [("Check for updates", "ElectriDrive is up to date.")]
    parent.close()


def test_manual_check_reports_network_error(monkeypatch, qapp):
    controller, parent = _controller(qapp, TimeoutError("offline"))
    dialogs = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, title, message: dialogs.append((title, message)),
    )

    controller.check_manually()

    assert len(dialogs) == 1
    assert dialogs[0][0] == "Update check failed"
    assert "network connection" in dialogs[0][1]
    parent.close()


def test_manual_check_routes_newer_release_to_update_dialog(monkeypatch, qapp):
    response = [
        {"tag_name": "v2.1.1", "draft": False, "prerelease": False}
    ]
    controller, parent = _controller(qapp, response)
    shown = []
    monkeypatch.setattr(controller, "_show_available", shown.append)

    controller.check_manually()

    assert [str(release.version) for release in shown] == ["2.1.1"]
    parent.close()
