"""The packaged (PyInstaller) binary is built from app.py, not electridrive.cli.
Any subcommand other than the historical --doctor flag must still reach the
real CLI instead of silently launching the GUI (see CLAUDE mission notes:
QA root creation and scripted/systemd sync only ever worked from source)."""
import importlib.util
import sys
from pathlib import Path

from electridrive import qa_policy as qa


def _load_app_module():
    spec = importlib.util.spec_from_file_location(
        "electridrive_app_entry", Path(__file__).resolve().parents[1] / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_doctor_flag_dispatches_to_cli_doctor(monkeypatch):
    app_entry = _load_app_module()
    from electridrive import cli
    calls = []
    monkeypatch.setattr(cli, "main", lambda argv: calls.append(list(argv)) or 0)
    monkeypatch.setattr(sys, "argv", ["electridrive", "--doctor"])
    assert app_entry.main() == 0
    assert calls == [["doctor"]]


def test_subcommand_dispatches_to_cli_without_starting_gui(monkeypatch):
    app_entry = _load_app_module()
    from electridrive import cli
    from electridrive.ui import app as gui_app
    calls = []
    monkeypatch.setattr(cli, "main", lambda argv: calls.append(list(argv)) or 0)
    monkeypatch.setattr(gui_app, "run_app",
                         lambda: (_ for _ in ()).throw(AssertionError("GUI must not start")))
    monkeypatch.setattr(sys, "argv", ["electridrive", "list", "--limit", "5"])
    assert app_entry.main() == 0
    assert calls == [["list", "--limit", "5"]]


def test_qa_profile_flag_is_consumed_before_cli_dispatch(monkeypatch):
    app_entry = _load_app_module()
    from electridrive import cli
    seen = []
    monkeypatch.setattr(qa, "bootstrap_profile", lambda argv: seen.append(argv) or argv[2:])
    monkeypatch.setattr(cli, "main", lambda argv: seen.append(("cli", argv)) or 0)
    monkeypatch.setattr(sys, "argv", ["electridrive", "--qa-profile", "/tmp/x", "doctor"])
    assert app_entry.main() == 0
    assert seen[0] == ["--qa-profile", "/tmp/x", "doctor"]
    assert seen[1] == ("cli", ["doctor"])


def test_bare_launch_starts_gui(monkeypatch):
    app_entry = _load_app_module()
    from electridrive.ui import app as gui_app
    monkeypatch.setattr(qa, "bootstrap_profile", lambda argv: argv)
    started = []
    monkeypatch.setattr(gui_app, "run_app", lambda: started.append(True) or 0)
    monkeypatch.setattr(sys, "argv", ["electridrive"])
    assert app_entry.main() == 0
    assert started == [True]
