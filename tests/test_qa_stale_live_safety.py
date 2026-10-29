"""Offline regression checks for the PR19 real-Drive QA runner's guardrails."""
from __future__ import annotations

import json

import pytest

from electridrive.config import Settings, SyncPair
from scripts import qa_stale_canonical_live as live


def test_readonly_live_guard_rejects_all_engine_mutation_methods():
    class Fake:
        def list_folder(self, parent, page):
            return (parent, page)

        def find_file(self, name, parent):
            return None

    guard = live.NoWriteClient(Fake(), "qa-folder-id", "ElectriDrive-QA-DUP-test")
    assert guard.ensure_folder_path("ElectriDrive-QA-DUP-test") == "qa-folder-id"
    assert guard.list_folder("qa-folder-id") == ("qa-folder-id", None)
    assert guard.find_file("duplicate.txt", "qa-folder-id") is None

    for attr in ("create_folder", "upload_file", "update_file", "download_file",
                 "trash", "untrash", "export_file"):
        with pytest.raises(RuntimeError):
            getattr(guard, attr)()
    with pytest.raises(RuntimeError):
        _ = guard.service
    with pytest.raises(AttributeError):
        _ = guard.undefined_operation
    with pytest.raises(RuntimeError):
        guard.list_folder("root")
    with pytest.raises(RuntimeError):
        guard.ensure_folder_path("ElectriDrive/Dokumente")
    assert "update_file" in guard.unsafe_calls
    assert "unexpected-list" in guard.unsafe_calls


def test_live_runner_requires_one_disabled_synthetic_pair(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setattr(live, "SOURCE", source)
    settings_path = tmp_path / "settings.json"
    state = {"remote_folder": "ElectriDrive-QA-DUP-test", "candidate_ids": ["id-a", "id-b"]}
    pair = SyncPair(str(source), state["remote_folder"], direction="up_only",
                    delete_policy="off", enabled=False, include_all_files=True,
                    canonical_remote_ids={"duplicate.txt": "id-a"})
    settings_path.write_text(json.dumps(Settings(scope="file", sync_pairs=[pair]).to_dict()))
    verified_pair, chosen = live.validate_settings(settings_path, state)
    assert chosen == "id-a"
    assert verified_pair.enabled is False

    personal = SyncPair(str(tmp_path / "Dokumente"), "ElectriDrive/Dokumente",
                        direction="up_only", enabled=True)
    settings_path.write_text(
        json.dumps(Settings(scope="file", sync_pairs=[pair, personal]).to_dict()))
    with pytest.raises(SystemExit, match="extra pairs"):
        live.validate_settings(settings_path, state)

    pair.enabled = True
    settings_path.write_text(json.dumps(Settings(scope="file", sync_pairs=[pair]).to_dict()))
    with pytest.raises(SystemExit, match="isolated disabled"):
        live.validate_settings(settings_path, state)


def test_live_qa_recovery_record_never_overwrites(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "RECOVERY", tmp_path / "recovery.json")
    live.write_recovery({"selected_id": "synthetic"})
    with pytest.raises(FileExistsError):
        live.write_recovery({"selected_id": "wrong"})
    assert json.loads(live.RECOVERY.read_text()) == {"selected_id": "synthetic"}
