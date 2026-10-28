"""QA live runner must never load or sync an actual personal root."""
from __future__ import annotations

from pathlib import Path

import pytest

from electridrive.config import Settings, SyncPair
from scripts import qa_duplicate_live as qa


@pytest.fixture
def synthetic_qa(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(qa, "QA", tmp_path / "ElectriDrive-DUP-LIVE")
    monkeypatch.setattr(qa, "SOURCE", qa.QA / "source")
    state = {
        "remote_folder": "ElectriDrive-QA-DUP-testfixture",
        "candidate_ids": ["candidate-1", "candidate-2"],
    }
    pair = SyncPair(
        local_path=str(qa.SOURCE),
        remote_folder=state["remote_folder"],
        direction="up_only", enabled=False,
        delete_policy="off", include_all_files=True,
    )
    return state, pair


def test_qa_profile_rejects_real_document_pair(synthetic_qa):
    state, pair = synthetic_qa
    personal = SyncPair(
        local_path="/home/user/Dokumente/Dokumente",
        remote_folder="ElectriDrive/Dokumente",
        direction="up_only", enabled=True,
    )
    with pytest.raises(SystemExit, match="QA-Profil nicht isoliert"):
        qa.validate_qa_settings(
            Settings(scope="file", sync_pairs=[pair, personal]),
            state, allow_enabled=False,
        )


def test_qa_profile_accepts_only_one_disabled_pair(synthetic_qa):
    state, pair = synthetic_qa
    qa.validate_qa_settings(
        Settings(scope="file", sync_pairs=[pair]),
        state, allow_enabled=False,
    )


@pytest.mark.parametrize("field", [
    "local_path", "remote_folder", "direction", "delete_policy",
    "include_all_files",
])
def test_qa_profile_rejects_tampered_pair(synthetic_qa, field):
    state, pair = synthetic_qa
    bad = {
        "local_path": "/home/user/Dokumente/Dokumente",
        "remote_folder": "ElectriDrive/Dokumente",
        "direction": "two_way",
        "delete_policy": "trash",
        "include_all_files": False,
    }
    setattr(pair, field, bad[field])
    with pytest.raises(SystemExit, match="QA-Profil nicht isoliert"):
        qa.validate_qa_settings(Settings(scope="file", sync_pairs=[pair]),
                                state, allow_enabled=False)


def test_qa_profile_rejects_enabled_pair_on_initial_launch(synthetic_qa):
    state, pair = synthetic_qa
    pair.enabled = True
    with pytest.raises(SystemExit, match="QA-Testpaar ist noch aktiviert"):
        qa.validate_qa_settings(Settings(scope="file", sync_pairs=[pair]),
                                state, allow_enabled=False)


def test_qa_profile_rejects_unknown_canonical_id(synthetic_qa):
    state, pair = synthetic_qa
    pair.canonical_remote_ids["duplicate.txt"] = "some-other-real-drive-id"
    with pytest.raises(SystemExit, match="nicht geprüfte Drive-Datei-ID"):
        qa.validate_qa_settings(Settings(scope="file", sync_pairs=[pair]),
                                state, allow_enabled=False)


def test_qa_profile_allows_only_qa_canonical_id(synthetic_qa):
    state, pair = synthetic_qa
    pair.canonical_remote_ids["duplicate.txt"] = "candidate-2"
    qa.validate_qa_settings(Settings(scope="file", sync_pairs=[pair]),
                            state, allow_enabled=False)


def test_qa_profile_rejects_unexpected_access_scope(synthetic_qa):
    state, pair = synthetic_qa
    with pytest.raises(SystemExit, match="QA-Profil nicht isoliert"):
        qa.validate_qa_settings(Settings(scope="drive", sync_pairs=[pair]),
                                state, allow_enabled=False)
