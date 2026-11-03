"""Synthetic-only tests of the persistent QA capability boundary."""
from pathlib import Path
import json
import os

import pytest

from electridrive import qa_policy as qa
from electridrive.config import Settings, SyncPair, get_paths, load_settings, save_settings


@pytest.fixture
def sealed(monkeypatch, tmp_path):
    monkeypatch.setattr(qa, "_active", None)
    monkeypatch.delenv("ELECTRIDRIVE_QA_PROFILE", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "ordinary-config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "ordinary-state"))
    policy = qa.initialize(tmp_path / "profile", tmp_path / "synthetic")
    monkeypatch.setenv("ELECTRIDRIVE_QA_PROFILE", str(policy.profile))
    assert qa.current_policy() is not None
    return qa.current_policy()


def test_normal_mode_remains_opt_in(monkeypatch, tmp_path):
    monkeypatch.setattr(qa, "_active", None)
    monkeypatch.delenv("ELECTRIDRIVE_QA_PROFILE", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "normal"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert qa.current_policy() is None
    assert get_paths().config_dir == tmp_path / "normal/electridrive"


def test_profile_never_imports_ordinary_pairs_or_paths(sealed, monkeypatch, tmp_path):
    ordinary = tmp_path / "ordinary-config/electridrive"
    ordinary.mkdir(parents=True)
    content = json.dumps({"sync_pairs": [{"local_path": "/synthetic-personal", "remote_folder": "Personal"}]})
    (ordinary / "settings.json").write_text(content)
    assert get_paths().config_dir == sealed.profile / "config/electridrive"
    assert load_settings().sync_pairs == []
    assert (ordinary / "settings.json").read_text() == content


def test_marker_reactivates_mode_without_environment_flag(sealed, monkeypatch):
    monkeypatch.setattr(qa, "_active", None)
    monkeypatch.delenv("ELECTRIDRIVE_QA_PROFILE")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(sealed.profile / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(sealed.profile / "state"))
    assert qa.current_policy().profile == sealed.profile


@pytest.mark.parametrize("relative", [True, False])
def test_outside_or_relative_sources_are_denied(sealed, tmp_path, relative):
    outside = tmp_path / "personal"
    outside.mkdir()
    selected = Path("../synthetic") if relative else outside
    with pytest.raises(qa.QaSafetyError):
        qa.require_local(selected)


def test_symlink_and_hardlink_cannot_import_personal_bytes(sealed, tmp_path):
    personal = tmp_path / "personal.txt"
    personal.write_bytes(b"private fixture, never upload")
    link = sealed.local_root / "link"
    link.symlink_to(personal)
    with pytest.raises(qa.QaSafetyError):
        with qa.open_source(link):
            pytest.fail("unsafe read")
    hard = sealed.local_root / "hard"
    os.link(personal, hard)
    with pytest.raises(qa.QaSafetyError):
        with qa.open_source(hard):
            pytest.fail("unsafe hardlink read")
    assert personal.read_bytes() == b"private fixture, never upload"


def test_valid_source_can_be_read_and_copied_safely(sealed):
    source = sealed.local_root / "data.txt"
    source.write_bytes(b"artificial")
    with qa.open_source(source) as handle:
        assert handle.read() == b"artificial"
    qa.copy_file(source, sealed.local_root / "copy.txt")
    assert (sealed.local_root / "copy.txt").read_bytes() == b"artificial"


def test_policy_tampering_cannot_expand_root_or_disable_latched_mode(sealed, monkeypatch, tmp_path):
    monkeypatch.delenv("ELECTRIDRIVE_QA_PROFILE")
    path = sealed.profile / ".electridrive-qa.json"
    data = json.loads(path.read_text())
    data["local_root"] = str(tmp_path)
    path.write_text(json.dumps(data))
    with pytest.raises(qa.QaSafetyError):
        get_paths()


def test_missing_marker_is_fatal_not_default_settings(sealed):
    (sealed.profile / "config/electridrive/qa-mode.json").unlink()
    with pytest.raises(qa.QaSafetyError):
        load_settings()


def test_tampered_settings_are_rejected_even_for_disabled_pairs(sealed, tmp_path):
    path = get_paths().settings_file
    path.write_text(json.dumps(Settings(sync_pairs=[SyncPair(
        str(tmp_path / "personal"), "Personal", enabled=False,
    )]).to_dict()))
    with pytest.raises(qa.QaSafetyError):
        load_settings()


def test_settings_cannot_enable_full_scope_or_arbitrary_cloud_names(sealed):
    with pytest.raises(qa.QaSafetyError):
        save_settings(Settings(scope="drive"))
    with pytest.raises(qa.QaSafetyError):
        qa.validate_pair(SyncPair(str(sealed.local_root), "ElectriDrive-QA-lookalike", delete_policy="off"))


def test_initialization_refuses_nonempty_or_overlapping_roots(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "existing.txt").write_bytes(b"do not adopt")
    with pytest.raises(qa.QaSafetyError):
        qa.initialize(tmp_path / "profile", data)
    with pytest.raises(qa.QaSafetyError):
        qa.initialize(tmp_path / "other", tmp_path / "other/data")


@pytest.fixture
def qa_remote(sealed):
    from dataclasses import replace
    from fakes import FakeDrive
    from electridrive.qa_drive import QaDriveClient

    class SyntheticDrive(FakeDrive):
        def _to_remote(self, node):
            return replace(super()._to_remote(node), app_properties=node.get("app_properties", {}))

    fake = SyntheticDrive()
    rid = fake.add_folder("ElectriDrive-QA-artificial")
    fake.nodes[rid]["app_properties"] = {"electridrive_qa_nonce": sealed.nonce}
    sealed.register_new_root(fake.get_metadata(rid), "ElectriDrive-QA-artificial")
    return fake, QaDriveClient(fake, sealed), rid


def test_registered_id_and_nonce_anchor_cloud_scope(qa_remote, sealed):
    fake, guarded, rid = qa_remote
    other = fake.add_folder("ElectriDrive-QA-artificial")
    assert guarded.ensure_folder_path("qa:" + rid) == rid
    assert {f.id for f in guarded.list_folder("root").files} == {rid}
    with pytest.raises(qa.QaSafetyError):
        guarded.ensure_folder_path("ElectriDrive-QA-artificial")
    with pytest.raises(qa.QaSafetyError):
        guarded.create_folder("escape", other)
    with pytest.raises(qa.QaSafetyError):
        guarded.get_metadata(other)


@pytest.mark.parametrize("change", ["nonce", "parents", "name", "trashed"])
def test_remote_root_change_stops_before_write(qa_remote, sealed, change):
    fake, guarded, rid = qa_remote
    source = sealed.local_root / "sample.txt"
    source.write_bytes(b"synthetic")
    if change == "nonce":
        fake.nodes[rid]["app_properties"] = {}
    elif change == "parents":
        fake.nodes[rid]["parent"] = fake.add_folder("other")
    else:
        fake.nodes[rid][change] = True if change == "trashed" else "changed"
    with pytest.raises(qa.QaSafetyError):
        guarded.upload_file(source, rid, source.name)
    assert not fake.uploads


def test_reparented_child_and_deletion_are_denied(qa_remote, sealed):
    fake, guarded, rid = qa_remote
    fid = fake.add_file("sample.txt", b"synthetic", parent=rid)
    guarded.list_folder(rid)
    fake.nodes[fid]["parent"] = fake.add_folder("outside")
    source = sealed.local_root / "sample.txt"
    source.write_bytes(b"changed")
    with pytest.raises(qa.QaSafetyError):
        guarded.update_file(fid, source)
    for method in (guarded.trash, guarded.untrash):
        with pytest.raises(qa.QaSafetyError):
            method(fid)
    with pytest.raises(qa.QaSafetyError):
        _ = guarded.service
    assert not fake.updates and not fake.nodes[fid]["trashed"]


def test_atomic_destination_refuses_links_without_changing_outside(sealed, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"untouched fixture")
    dest = sealed.local_root / "output.txt"
    dest.symlink_to(outside)
    with pytest.raises(qa.QaSafetyError):
        with sealed.atomic_writer(dest) as out:
            out.write(b"must never escape")
    assert outside.read_bytes() == b"untouched fixture"


def test_atomic_destination_rechecks_link_swapped_while_downloading(sealed, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"unchanged")
    dest = sealed.local_root / "output.txt"
    with pytest.raises(qa.QaSafetyError):
        with sealed.atomic_writer(dest) as out:
            out.write(b"synthetic download")
            dest.symlink_to(outside)
    assert outside.read_bytes() == b"unchanged"
    assert not list(sealed.local_root.glob(".qa-transfer-*"))


def test_atomic_destination_ancestor_swap_cannot_write_outside(sealed, tmp_path):
    directory = sealed.local_root / "sub"
    directory.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(qa.QaSafetyError):
        with sealed.atomic_writer(directory / "output.txt") as out:
            out.write(b"synthetic")
            directory.rename(sealed.local_root / "moved")
            directory.symlink_to(outside, target_is_directory=True)
    assert not (outside / "output.txt").exists()


def test_profile_read_refuses_swapped_final_symlink(sealed, tmp_path, monkeypatch):
    target = sealed.profile / "config/electridrive/fixture.json"
    target.write_text("{}")
    outside = tmp_path / "outside.json"
    outside.write_text('{"private-fixture":true}')
    original = qa.check_profile_file

    def swap(path, **kwargs):
        original(path, **kwargs)
        target.unlink()
        target.symlink_to(outside)

    monkeypatch.setattr(qa, "check_profile_file", swap)
    with pytest.raises(qa.QaSafetyError):
        qa._read(target, strict=False)


def test_conflicting_bootstrap_flags_cannot_switch_profile(sealed, tmp_path):
    with pytest.raises(qa.QaSafetyError):
        qa.bootstrap_profile(["--qa-profile", str(sealed.profile), "--qa-profile", str(tmp_path / "other")])
    with pytest.raises(qa.QaSafetyError):
        qa.bootstrap_profile(["--qa-profile", str(tmp_path / "other")])
    assert qa.current_policy() is sealed


def test_partial_initialization_does_not_overwrite_existing_marker(tmp_path):
    profile = tmp_path / "profile"
    marker = profile / "config/electridrive/qa-mode.json"
    marker.parent.mkdir(parents=True)
    profile.chmod(0o700)
    marker.write_text("fixture-marker")
    with pytest.raises(qa.QaSafetyError):
        qa.initialize(profile, tmp_path / "synthetic")
    assert marker.read_text() == "fixture-marker"


def test_download_parent_moved_outside_and_replaced_cannot_publish(sealed, tmp_path):
    directory = sealed.local_root / 'sub'
    directory.mkdir()
    outside = tmp_path / 'moved-outside'
    with pytest.raises(qa.QaSafetyError):
        with sealed.atomic_writer(directory / 'output.txt') as out:
            out.write(b'artificial download')
            directory.rename(outside)
            directory.mkdir()
    assert not (outside / 'output.txt').exists()
    assert not list(outside.iterdir())


def test_log_replacement_symlink_never_appends_outside(sealed, tmp_path, monkeypatch):
    import logging
    from electridrive.logging_setup import JsonlHandler
    path = get_paths().log_file
    handler = JsonlHandler(path)
    outside = tmp_path/'outside.log'
    outside.write_bytes(b'unchanged')
    path.symlink_to(outside)
    rejected = []
    monkeypatch.setattr(handler, 'handleError', lambda record: rejected.append(True))
    handler.emit(logging.LogRecord('qa-fixture', logging.INFO, '', 1, 'synthetic', (), None))
    assert outside.read_bytes() == b'unchanged'
    assert rejected


@pytest.mark.parametrize('malformed', [
    {'sync_pairs':['invalid']}, {'sync_pairs':{}}, {'sync_pairs':None},
    {'sync_pairs':[{'local_path': '/outside', 'remote_folder':'qa:id', 'enabled':'false'}]},
    [],
])
def test_malformed_qa_settings_never_silently_become_defaults(sealed, malformed):
    get_paths().settings_file.write_text(json.dumps(malformed))
    with pytest.raises(qa.QaSafetyError):
        load_settings()
