from pathlib import Path
from datetime import datetime, timedelta, timezone
import os

import pytest

from electridrive.config import Settings, SyncPair
from electridrive.storage.database import SyncDatabase
from electridrive.sync.twoway import (
    ActionKind,
    LastEntry,
    LocalEntry,
    RemoteEntry,
    TwoWaySyncEngine,
    reconcile,
)
from fakes import FakeDrive


def L(rel, md5, mtime_ns=1_000_000_000_000_000_000):
    return LocalEntry(rel, mtime_ns, 10, md5)


def R(rel, md5, modified="2024-01-01T00:00:00Z"):
    return RemoteEntry(rel, f"id-{rel}", modified, md5, 10)


def S(rel, lmd5, rmd5):
    return LastEntry(rel, lmd5, rmd5)


def kinds(actions):
    return {a.rel: a.kind for a in actions}


# ----------------------------------------------------------------- pure matrix
def test_new_local_uploads():
    assert kinds(reconcile({"a": L("a", "x")}, {}, {})) == {"a": ActionKind.UPLOAD}


def test_new_remote_downloads():
    assert kinds(reconcile({}, {"a": R("a", "x")}, {})) == {"a": ActionKind.DOWNLOAD}


def test_identical_without_state_records():
    acts = reconcile({"a": L("a", "x")}, {"a": R("a", "x")}, {})
    assert kinds(acts) == {"a": ActionKind.RECORD}


def test_identical_with_matching_state_is_noop():
    acts = reconcile({"a": L("a", "x")}, {"a": R("a", "x")}, {"a": S("a", "x", "x")})
    assert acts == []


def test_local_changed_uploads():
    acts = reconcile({"a": L("a", "new")}, {"a": R("a", "old")}, {"a": S("a", "old", "old")})
    assert kinds(acts) == {"a": ActionKind.UPLOAD}


def test_remote_changed_downloads():
    acts = reconcile({"a": L("a", "old")}, {"a": R("a", "new")}, {"a": S("a", "old", "old")})
    assert kinds(acts) == {"a": ActionKind.DOWNLOAD}


def test_both_changed_conflicts():
    acts = reconcile({"a": L("a", "ln")}, {"a": R("a", "rn")}, {"a": S("a", "lo", "ro")})
    assert acts[0].kind == ActionKind.CONFLICT


def test_local_deleted_trashes_remote():
    acts = reconcile({}, {"a": R("a", "x")}, {"a": S("a", "x", "x")})
    assert kinds(acts) == {"a": ActionKind.TRASH_REMOTE}


def test_remote_deleted_trashes_local():
    acts = reconcile({"a": L("a", "x")}, {}, {"a": S("a", "x", "x")})
    assert kinds(acts) == {"a": ActionKind.TRASH_LOCAL}


def test_delete_policy_off_forgets_instead_of_trashing():
    acts = reconcile({}, {"a": R("a", "x")}, {"a": S("a", "x", "x")}, delete_policy="off")
    assert kinds(acts) == {"a": ActionKind.FORGET}


def test_no_reconcile_action_is_a_permanent_delete():
    # The only deletion kinds are trash (recoverable). There is no permanent delete.
    acts = reconcile({"a": L("a", "x")}, {}, {"a": S("a", "x", "x")})
    assert all(a.kind != "delete" for a in acts)


def test_up_only_direction_blocks_local_changes():
    acts = reconcile({}, {"a": R("a", "x")}, {}, direction="up_only")
    assert acts == []  # would be a DOWNLOAD in two-way; blocked here


# ------------------------------------------------------------- engine end-to-end
def _pair(local_root: Path) -> SyncPair:
    return SyncPair(local_path=str(local_root), remote_folder="ElectriDrive/SyncTest",
                    direction="two_way", delete_policy="trash")


def test_engine_initial_sync_then_idempotent(tmp_path: Path):
    root = tmp_path / "sync"
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_text("alpha", encoding="utf-8")
    (root / "sub" / "b.txt").write_text("beta", encoding="utf-8")

    fake = FakeDrive()
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        engine = TwoWaySyncEngine(fake, db, _pair(root))
        r1 = engine.run()
        assert r1.uploaded == 2 and r1.failed == 0

        r2 = engine.run()
        assert (r2.uploaded, r2.downloaded, r2.trashed_remote, r2.trashed_local) == (0, 0, 0, 0)
    finally:
        db.close()


def test_engine_downloads_new_remote_and_propagates_local_delete(tmp_path: Path):
    root = tmp_path / "sync"
    root.mkdir()
    (root / "a.txt").write_text("alpha", encoding="utf-8")

    fake = FakeDrive()
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        engine = TwoWaySyncEngine(fake, db, _pair(root))
        engine.run()  # uploads a.txt

        # a new remote file appears under the synced folder
        remote_root = fake.ensure_folder_path("ElectriDrive/SyncTest")
        fake.add_file("c.txt", b"gamma", parent=remote_root)
        r = engine.run()
        assert r.downloaded == 1
        assert (root / "c.txt").read_text(encoding="utf-8") == "gamma"

        # delete a local file -> remote counterpart goes to Drive trash
        (root / "a.txt").unlink()
        r = engine.run()
        assert r.trashed_remote == 1
    finally:
        db.close()


def test_engine_local_edit_updates_existing_remote_id(tmp_path: Path):
    root = tmp_path / "sync"
    root.mkdir()
    local_file = root / "a.txt"
    local_file.write_text("first", encoding="utf-8")

    fake = FakeDrive()
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        engine = TwoWaySyncEngine(fake, db, _pair(root))
        assert engine.run().uploaded == 1
        original = db.get_sync_item(engine.pair_id, "a.txt")
        assert original is not None

        local_file.write_text("second", encoding="utf-8")
        report = engine.run()
        current = db.get_sync_item(engine.pair_id, "a.txt")

        assert report.uploaded == 1 and report.failed == 0
        assert len(fake.uploads) == 1
        assert fake.updates[-1][0] == original.remote_id
        assert current is not None and current.remote_id == original.remote_id
        canonical = [
            n for n in fake.nodes.values()
            if n["name"] == "a.txt" and not n["trashed"]
        ]
        assert [(n["id"], n["content"]) for n in canonical] == [
            (original.remote_id, b"second")
        ]
    finally:
        db.close()


def test_local_winner_conflict_updates_canonical_remote_without_duplicate(tmp_path: Path):
    root = tmp_path / "sync"
    root.mkdir()
    local_file = root / "a.txt"
    local_file.write_text("base", encoding="utf-8")

    fake = FakeDrive()
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        engine = TwoWaySyncEngine(fake, db, _pair(root))
        engine.run()
        original = db.get_sync_item(engine.pair_id, "a.txt")
        assert original is not None

        fake.modify_file(
            original.remote_id,
            b"remote edit",
            modified="2024-01-03T00:00:00Z",
        )
        local_file.write_text("local edit", encoding="utf-8")
        report = engine.run()

        assert report.conflicts == 1 and report.failed == 0
        # Initial canonical upload plus the preserved remote conflict copy. The
        # latter is synchronized in the same run instead of being left local-only.
        assert len(fake.uploads) == 2
        assert fake.updates[-1][0] == original.remote_id
        canonical = [
            n for n in fake.nodes.values()
            if n["name"] == "a.txt" and not n["trashed"]
        ]
        assert len(canonical) == 1
        assert canonical[0]["id"] == original.remote_id
        assert canonical[0]["content"] == b"local edit"
        conflict_files = list(root.glob("a (conflict *).txt"))
        assert len(conflict_files) == 1
        assert conflict_files[0].read_bytes() == b"remote edit"
        conflict_remote = [
            node
            for node in fake.nodes.values()
            if node["name"] == conflict_files[0].name and not node["trashed"]
        ]
        assert len(conflict_remote) == 1
        assert conflict_remote[0]["content"] == b"remote edit"
    finally:
        db.close()


@pytest.mark.parametrize(
    ("mime_type", "extension"),
    [
        ("application/vnd.google-apps.document", ".docx"),
        ("application/vnd.google-apps.spreadsheet", ".xlsx"),
        ("application/vnd.google-apps.presentation", ".pptx"),
    ],
)
def test_workspace_file_exports_with_stable_extension_and_is_idempotent(
    tmp_path: Path, mime_type: str, extension: str
):
    root = tmp_path / "sync"
    root.mkdir()
    fake = FakeDrive()
    remote_root = fake.ensure_folder_path("ElectriDrive/SyncTest")
    native_id = fake.add_file("Quarterly", b"v1", parent=remote_root, mime=mime_type)
    db = SyncDatabase(tmp_path / f"state-{extension[1:]}.sqlite3")
    try:
        engine = TwoWaySyncEngine(fake, db, _pair(root))
        first = engine.run()
        exported = root / f"Quarterly{extension}"

        assert first.downloaded == 1 and first.failed == 0
        assert exported.read_bytes() == b"EXPORTED:v1"
        state = db.get_sync_item(engine.pair_id, exported.name)
        assert state is not None and state.remote_id == native_id

        second = engine.run()
        assert (
            second.uploaded,
            second.downloaded,
            second.conflicts,
            second.failed,
        ) == (0, 0, 0, 0)
        assert fake.uploads == [] and fake.updates == []
    finally:
        db.close()


def test_workspace_remote_edit_reexports_and_local_edit_is_preserved(tmp_path: Path):
    root = tmp_path / "sync"
    root.mkdir()
    fake = FakeDrive()
    remote_root = fake.ensure_folder_path("ElectriDrive/SyncTest")
    native_id = fake.add_file(
        "Notes",
        b"remote-v1",
        parent=remote_root,
        mime="application/vnd.google-apps.document",
    )
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        engine = TwoWaySyncEngine(fake, db, _pair(root))
        engine.run()
        exported = root / "Notes.docx"

        fake.modify_file(native_id, b"remote-v2", modified="2024-02-01T00:00:00Z")
        remote_report = engine.run()
        assert remote_report.downloaded == 1
        assert exported.read_bytes() == b"EXPORTED:remote-v2"

        exported.write_bytes(b"local Office edit")
        local_report = engine.run()
        assert local_report.conflicts == 1 and local_report.failed == 0
        assert exported.read_bytes() == b"EXPORTED:remote-v2"
        preserved = list(root.glob("Notes (conflict *).docx"))
        assert len(preserved) == 1
        assert preserved[0].read_bytes() == b"local Office edit"
        assert fake.nodes[native_id]["content"] == b"remote-v2"
        # The edited export is retained as a separate binary conflict object;
        # the native Workspace file itself is never media-updated.
        assert fake.updates == []
        assert len(fake.uploads) == 1
        assert fake.uploads[0][2] == preserved[0].name
    finally:
        db.close()


def test_deleting_workspace_export_restores_it_without_trashing_native(tmp_path: Path):
    root = tmp_path / "sync"
    root.mkdir()
    fake = FakeDrive()
    remote_root = fake.ensure_folder_path("ElectriDrive/SyncTest")
    native_id = fake.add_file(
        "Plan",
        b"remote",
        parent=remote_root,
        mime="application/vnd.google-apps.document",
    )
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        engine = TwoWaySyncEngine(fake, db, _pair(root))
        engine.run()
        exported = root / "Plan.docx"
        exported.unlink()

        report = engine.run()
        assert report.downloaded == 1 and report.trashed_remote == 0
        assert exported.read_bytes() == b"EXPORTED:remote"
        assert fake.nodes[native_id]["trashed"] is False
    finally:
        db.close()


def test_workspace_export_and_same_named_binary_are_both_synced(tmp_path: Path):
    root = tmp_path / "sync"
    root.mkdir()
    fake = FakeDrive()
    remote_root = fake.ensure_folder_path("ElectriDrive/SyncTest")
    native_id = fake.add_file(
        "Notes",
        b"native",
        parent=remote_root,
        mime="application/vnd.google-apps.document",
    )
    binary_id = fake.add_file("Notes.docx", b"binary", parent=remote_root)
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        engine = TwoWaySyncEngine(fake, db, _pair(root))
        first = engine.run()

        assert first.downloaded == 2 and first.failed == 0
        assert (root / "Notes.docx").read_bytes() == b"binary"
        assert (root / "Notes (Google Doc).docx").read_bytes() == b"EXPORTED:native"
        binary_state = db.get_sync_item(engine.pair_id, "Notes.docx")
        native_state = db.get_sync_item(engine.pair_id, "Notes (Google Doc).docx")
        assert binary_state is not None and binary_state.remote_id == binary_id
        assert native_state is not None and native_state.remote_id == native_id

        second = engine.run()
        assert (
            second.uploaded,
            second.downloaded,
            second.conflicts,
            second.failed,
        ) == (0, 0, 0, 0)
    finally:
        db.close()


def test_new_binary_collision_preserves_existing_workspace_export_path(tmp_path: Path):
    root = tmp_path / "sync"
    root.mkdir()
    fake = FakeDrive()
    remote_root = fake.ensure_folder_path("ElectriDrive/SyncTest")
    native_id = fake.add_file(
        "Notes",
        b"native",
        parent=remote_root,
        mime="application/vnd.google-apps.document",
    )
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        engine = TwoWaySyncEngine(fake, db, _pair(root))
        engine.run()
        original = db.get_sync_item(engine.pair_id, "Notes.docx")
        assert original is not None and original.remote_id == native_id

        binary_id = fake.add_file("Notes.docx", b"binary", parent=remote_root)
        report = engine.run()

        assert report.downloaded == 1 and report.failed == 0
        assert (root / "Notes.docx").read_bytes() == b"EXPORTED:native"
        assert (root / "Notes (Drive file).docx").read_bytes() == b"binary"
        preserved = db.get_sync_item(engine.pair_id, "Notes.docx")
        newcomer = db.get_sync_item(engine.pair_id, "Notes (Drive file).docx")
        assert preserved is not None and preserved.remote_id == native_id
        assert newcomer is not None and newcomer.remote_id == binary_id

        assert engine.run().downloaded == 0

        # Editing the disambiguated local path updates binary media without
        # renaming the original Drive object to the local collision suffix.
        disambiguated = root / "Notes (Drive file).docx"
        disambiguated.write_bytes(b"binary edited locally")
        update_report = engine.run()
        assert update_report.uploaded == 1 and update_report.failed == 0
        assert fake.updates[-1] == (binary_id, str(disambiguated), None)
        assert fake.nodes[binary_id]["name"] == "Notes.docx"
        assert fake.nodes[binary_id]["content"] == b"binary edited locally"
    finally:
        db.close()


def test_changes_token_is_persisted_and_unchanged_snapshot_is_reused(tmp_path: Path):
    class CountingDrive(FakeDrive):
        def __init__(self):
            super().__init__()
            self.folder_list_calls = 0

        def list_folder(self, parent_id="root", page_token=None):
            self.folder_list_calls += 1
            return super().list_folder(parent_id, page_token)

    root = tmp_path / "sync"
    root.mkdir()
    fake = CountingDrive()
    remote_root = fake.ensure_folder_path("ElectriDrive/SyncTest")
    fake.add_file("remote.txt", b"remote", parent=remote_root)
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        engine = TwoWaySyncEngine(fake, db, _pair(root))
        engine.run()
        token = db.get_change_token(engine.change_token_key)
        calls_after_first = fake.folder_list_calls

        assert token is not None
        engine.run()
        assert fake.folder_list_calls == calls_after_first
        assert db.get_change_token(engine.change_token_key) == token
    finally:
        db.close()


def test_expired_changes_token_falls_back_to_full_scan_and_new_token(tmp_path: Path):
    class ExpiredTokenDrive(FakeDrive):
        def __init__(self):
            super().__init__()
            self.expired_attempts = 0

        def list_changes(self, page_token):
            if page_token == "expired":
                self.expired_attempts += 1
                raise RuntimeError("HTTP 410: page token expired")
            return super().list_changes(page_token)

    root = tmp_path / "sync"
    root.mkdir()
    fake = ExpiredTokenDrive()
    remote_root = fake.ensure_folder_path("ElectriDrive/SyncTest")
    fake.add_file("remote.txt", b"remote", parent=remote_root)
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        engine = TwoWaySyncEngine(fake, db, _pair(root))
        db.set_change_token(engine.change_token_key, "expired")

        report = engine.run()

        assert fake.expired_attempts == 1
        assert report.downloaded == 1 and report.failed == 0
        assert (root / "remote.txt").read_bytes() == b"remote"
        assert db.get_change_token(engine.change_token_key).startswith("tok-")
    finally:
        db.close()


def test_changes_api_failure_keeps_full_scan_correct(tmp_path: Path):
    class FailingChangesDrive(FakeDrive):
        def list_changes(self, page_token):
            raise OSError("changes endpoint unavailable")

    root = tmp_path / "sync"
    root.mkdir()
    fake = FailingChangesDrive()
    remote_root = fake.ensure_folder_path("ElectriDrive/SyncTest")
    fake.add_file("remote.txt", b"available via full scan", parent=remote_root)
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        engine = TwoWaySyncEngine(fake, db, _pair(root))

        report = engine.run()

        assert report.downloaded == 1 and report.failed == 0
        assert (root / "remote.txt").read_bytes() == b"available via full scan"
        # An unverifiable token window is never persisted as if it were safe.
        assert db.get_change_token(engine.change_token_key) is None
    finally:
        db.close()


# ------------------------------------------------------ upload-only duplicates
def test_up_only_ignores_unrelated_remote_duplicates_without_touching_them(
    tmp_path: Path,
):
    root = tmp_path / "uploads"
    root.mkdir()
    (root / "local.txt").write_bytes(b"safe upload")
    fake = FakeDrive()
    folder = fake.ensure_folder_path("ElectriDrive/SyncTest")
    first = fake.add_file("remote-only.txt", b"old-a", parent=folder)
    second = fake.add_file("remote-only.txt", b"old-b", parent=folder)
    db = SyncDatabase(tmp_path / "state.sqlite3")
    pair = SyncPair(str(root), "ElectriDrive/SyncTest", direction="up_only")
    try:
        report = TwoWaySyncEngine(fake, db, pair).run()
        assert report.failed == 0 and report.uploaded == 1
        assert len(report.remote_path_conflicts) == 1
        assert report.remote_path_conflicts[0].remote_path == "remote-only.txt"
        assert not report.remote_path_conflicts[0].resolved
        assert report.downloaded == report.trashed_local == report.trashed_remote == 0
        assert fake.nodes[first]["content"] == b"old-a"
        assert fake.nodes[second]["content"] == b"old-b"
        assert not fake.nodes[first]["trashed"]
        assert not fake.nodes[second]["trashed"]
        assert not (root / "remote-only.txt").exists()
        assert {item[2] for item in fake.uploads} == {"local.txt"}
        assert not fake.updates
    finally:
        db.close()


def test_up_only_refuses_ambiguous_existing_upload_target(tmp_path: Path):
    root = tmp_path / "uploads"
    root.mkdir()
    (root / "target.txt").write_bytes(b"my local truth")
    fake = FakeDrive()
    folder = fake.ensure_folder_path("ElectriDrive/SyncTest")
    first = fake.add_file("target.txt", b"one", parent=folder)
    second = fake.add_file("target.txt", b"two", parent=folder)
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        pair = SyncPair(str(root), "ElectriDrive/SyncTest", direction="up_only")
        report = TwoWaySyncEngine(fake, db, pair).run()
        assert report.failed >= 1
        assert any("ambiguous upload target" in e for e in report.errors)
        assert not fake.uploads and not fake.updates
        assert fake.nodes[first]["content"] == b"one"
        assert fake.nodes[second]["content"] == b"two"
        assert (root / "target.txt").read_bytes() == b"my local truth"
    finally:
        db.close()



def _utc_stamp(ago_days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=ago_days)).isoformat()


def _set_local_age(path: Path, days: float) -> None:
    stamp = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
    os.utime(path, (stamp, stamp))


def test_up_only_auto_updates_unique_newest_duplicate_when_local_is_newer(tmp_path: Path):
    root = tmp_path / "uploads"
    root.mkdir()
    local = root / "hetzner2026.odt"
    local.write_bytes(b"latest local document")
    _set_local_age(local, 1)
    fake = FakeDrive()
    folder = fake.ensure_folder_path("ElectriDrive/SyncTest")
    older = fake.add_file("hetzner2026.odt", b"old original", parent=folder,
                          modified=_utc_stamp(50))
    newest = fake.add_file("hetzner2026.odt", b"newer drive original",
                           parent=folder, modified=_utc_stamp(30))
    db = SyncDatabase(tmp_path / "state.sqlite3")
    pair = SyncPair(str(root), "ElectriDrive/SyncTest", direction="up_only", auto_select_newest_duplicate=True)
    try:
        engine = TwoWaySyncEngine(fake, db, pair)
        result = engine.run()
        assert result.failed == result.unexplained_omissions == 0
        assert result.auto_selected_duplicates == 1
        assert result.uploaded == 1 and result.downloaded == 0
        assert result.trashed_remote == result.trashed_local == 0
        assert result.remote_path_conflicts[0].resolved
        assert result.remote_path_conflicts[0].canonical_id == newest
        assert fake.updates == [(newest, str(local), None), (older, str(local), None)]
        assert result.updated_duplicate_files == 1
        assert not fake.uploads
        assert fake.nodes[newest]["content"] == b"latest local document"
        assert fake.nodes[older]["content"] == b"latest local document"
        assert not fake.nodes[older]["trashed"]
        assert db.get_sync_item(engine.pair_id, "hetzner2026.odt").remote_id == newest

        # The update records the immutable ID for the next process lifecycle.
        before = len(fake.updates)
        new_engine = TwoWaySyncEngine(fake, db, pair)
        second = new_engine.run()
        assert second.failed == 0 and second.uploaded == 0
        assert second.auto_selected_duplicates == 0
        assert len(fake.updates) == before
    finally:
        db.close()


@pytest.mark.parametrize("local_days,older_days,newer_days", [
    (40, 50, 30),  # Drive newest is actually newer than local
    (1, 30, 30),   # Tied remote dates cannot uniquely identify a target
])
def test_up_only_auto_selection_refuses_unsafe_timestamp_order(
    tmp_path: Path, local_days: float, older_days: float, newer_days: float,
):
    root = tmp_path / "uploads"
    root.mkdir()
    local = root / "same.txt"
    local.write_bytes(b"local data")
    _set_local_age(local, local_days)
    fake = FakeDrive()
    folder = fake.ensure_folder_path("ElectriDrive/SyncTest")
    older = fake.add_file("same.txt", b"first", parent=folder,
                          modified=_utc_stamp(older_days))
    newest = fake.add_file("same.txt", b"second", parent=folder,
                           modified=_utc_stamp(newer_days))
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        result = TwoWaySyncEngine(
            fake, db, SyncPair(str(root), "ElectriDrive/SyncTest", direction="up_only", auto_select_newest_duplicate=True)
        ).run()
        assert result.failed >= 1
        assert result.blocked_before_transfers
        assert result.auto_selected_duplicates == 0
        assert not fake.updates and not fake.uploads
        assert fake.nodes[older]["content"] == b"first"
        assert fake.nodes[newest]["content"] == b"second"
    finally:
        db.close()


def test_up_only_auto_selection_preserves_older_matching_local_version(tmp_path: Path):
    root = tmp_path / "uploads"
    root.mkdir()
    local = root / "same.txt"
    local.write_bytes(b"old matching content")
    _set_local_age(local, 1)
    fake = FakeDrive()
    folder = fake.ensure_folder_path("ElectriDrive/SyncTest")
    older = fake.add_file("same.txt", b"old matching content", parent=folder,
                          modified=_utc_stamp(50))
    newest = fake.add_file("same.txt", b"different newer content",
                           parent=folder, modified=_utc_stamp(30))
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        result = TwoWaySyncEngine(
            fake, db, SyncPair(str(root), "ElectriDrive/SyncTest", direction="up_only", auto_select_newest_duplicate=True)
        ).run()
        assert result.failed and result.blocked_before_transfers
        assert not fake.updates and not fake.uploads
        assert fake.nodes[older]["content"] == b"old matching content"
        assert fake.nodes[newest]["content"] == b"different newer content"
    finally:
        db.close()


def test_auto_adopt_rechecks_remote_metadata_before_overwrite(tmp_path: Path):
    root = tmp_path / "uploads"
    root.mkdir()
    local = root / "same.txt"
    local.write_bytes(b"local newest")
    _set_local_age(local, 1)
    fake = FakeDrive()
    folder = fake.ensure_folder_path("ElectriDrive/SyncTest")
    older = fake.add_file("same.txt", b"old", parent=folder,
                          modified=_utc_stamp(50))
    newest = fake.add_file("same.txt", b"slightly newer", parent=folder,
                           modified=_utc_stamp(30))
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        pair = SyncPair(str(root), "ElectriDrive/SyncTest", direction="up_only", auto_select_newest_duplicate=True)
        engine = TwoWaySyncEngine(fake, db, pair)
        original = engine._recheck_auto_update

        def race(rel, selected):
            # Simulate a different Drive client writing after the completed
            # inventory and before the Upload-only API update.
            fake.modify_file(newest, b"external newer content",
                             modified=_utc_stamp(0))
            original(rel, selected)

        engine._recheck_auto_update = race
        result = engine.run()
        assert result.failed >= 1
        assert result.uploaded == 0 and result.auto_selected_duplicates == 1
        assert fake.nodes[newest]["content"] == b"external newer content"
        assert fake.nodes[older]["content"] == b"old"
        assert not fake.updates and not fake.uploads
        assert db.get_sync_item(engine.pair_id, "same.txt") is None
    finally:
        db.close()


def test_up_only_auto_selects_after_full_paginated_duplicate_scan(tmp_path: Path):
    from electridrive.google_api.client import FileListing

    class Paged(FakeDrive):
        folder = ""

        def list_folder(self, parent_id="root", page_token=None):
            listing = super().list_folder(parent_id, None)
            if parent_id != self.folder:
                return listing
            return (FileListing(listing.files[:1], "next") if page_token is None
                    else FileListing(listing.files[1:], None))

    root = tmp_path / "uploads"
    root.mkdir()
    local = root / "same.txt"
    local.write_bytes(b"new local")
    _set_local_age(local, 1)
    fake = Paged()
    folder = fake.ensure_folder_path("ElectriDrive/SyncTest")
    fake.folder = folder
    older = fake.add_file("same.txt", b"old", parent=folder,
                          modified=_utc_stamp(50))
    newest = fake.add_file("same.txt", b"newer cloud", parent=folder,
                           modified=_utc_stamp(30))
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        result = TwoWaySyncEngine(
            fake, db, SyncPair(str(root), "ElectriDrive/SyncTest", direction="up_only", auto_select_newest_duplicate=True)
        ).run()
        assert result.failed == 0 and result.uploaded == 1
        assert fake.updates == [(newest, str(local), None), (older, str(local), None)]
        assert fake.nodes[older]["content"] == b"new local"
    finally:
        db.close()


def test_auto_newest_duplicates_is_opt_in_and_serialized(tmp_path: Path):
    root = tmp_path / "uploads"
    root.mkdir()
    local = root / "same.txt"
    local.write_bytes(b"latest local")
    _set_local_age(local, 1)
    fake = FakeDrive()
    folder = fake.ensure_folder_path("ElectriDrive/SyncTest")
    first = fake.add_file("same.txt", b"older", parent=folder,
                          modified=_utc_stamp(50))
    second = fake.add_file("same.txt", b"newer", parent=folder,
                           modified=_utc_stamp(30))
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        pair = SyncPair(str(root), "ElectriDrive/SyncTest", direction="up_only")
        assert not pair.auto_select_newest_duplicate
        assert not Settings.from_dict(Settings(sync_pairs=[pair]).to_dict()
                                      ).sync_pairs[0].auto_select_newest_duplicate
        blocked = TwoWaySyncEngine(fake, db, pair).run()
        assert blocked.failed and blocked.blocked_before_transfers
        assert not fake.updates and not fake.uploads
        assert fake.nodes[first]["content"] == b"older"
        assert fake.nodes[second]["content"] == b"newer"

        pair.auto_select_newest_duplicate = True
        persisted = Settings.from_dict(Settings(sync_pairs=[pair]).to_dict())
        assert persisted.sync_pairs[0].auto_select_newest_duplicate
        successful = TwoWaySyncEngine(fake, db, persisted.sync_pairs[0]).run()
        assert successful.failed == 0 and successful.uploaded == 1
        assert [item[0] for item in fake.updates] == [second, first]
        assert fake.nodes[first]["content"] == b"latest local"
    finally:
        db.close()


def test_existing_owner_selection_is_never_replaced_by_newest_heuristic(tmp_path: Path):
    root = tmp_path / "uploads"
    root.mkdir()
    local = root / "same.txt"
    local.write_bytes(b"latest local")
    _set_local_age(local, 1)
    fake = FakeDrive()
    folder = fake.ensure_folder_path("ElectriDrive/SyncTest")
    chosen_older = fake.add_file("same.txt", b"older user selected", parent=folder,
                                 modified=_utc_stamp(50))
    unselected_newer = fake.add_file("same.txt", b"newer unselected", parent=folder,
                                     modified=_utc_stamp(30))
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        pair = SyncPair(str(root), "ElectriDrive/SyncTest", direction="up_only",
                        auto_select_newest_duplicate=True,
                        canonical_remote_ids={"same.txt": chosen_older})
        result = TwoWaySyncEngine(fake, db, pair).run()
        assert result.failed == 0 and result.uploaded == 1
        assert result.auto_selected_duplicates == 0
        assert fake.updates == [(chosen_older, str(local), None)]
        assert fake.nodes[chosen_older]["content"] == b"latest local"
        assert fake.nodes[unselected_newer]["content"] == b"newer unselected"
    finally:
        db.close()


def test_latest_wins_two_way_upload_overwrites_old_cloud_without_conflict(tmp_path: Path):
    root = tmp_path / "downloads"
    root.mkdir()
    local = root / "same.txt"
    local.write_bytes(b"current local")
    _set_local_age(local, 1)
    fake = FakeDrive()
    folder = fake.ensure_folder_path("ElectriDrive/SyncTest")
    first = fake.add_file("same.txt", b"stale oldest", parent=folder,
                          modified=_utc_stamp(50))
    second = fake.add_file("same.txt", b"stale newer", parent=folder,
                           modified=_utc_stamp(30))
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        pair = SyncPair(str(root), "ElectriDrive/SyncTest",
                        direction="two_way", latest_wins=True)
        report = TwoWaySyncEngine(fake, db, pair).run()
        assert report.failed == 0
        assert report.uploaded == 1 and report.updated_duplicate_files == 1
        assert report.conflicts == 0
        assert not fake.uploads
        assert [x[0] for x in fake.updates] == [second, first]
        assert fake.nodes[first]["content"] == b"current local"
        assert fake.nodes[second]["content"] == b"current local"
        assert not list(root.glob("*conflict*"))
    finally:
        db.close()


def test_latest_wins_two_way_download_overwrites_old_local_and_old_cloud(tmp_path: Path):
    root = tmp_path / "downloads"
    root.mkdir()
    local = root / "same.txt"
    local.write_bytes(b"stale local")
    _set_local_age(local, 55)
    fake = FakeDrive()
    folder = fake.ensure_folder_path("ElectriDrive/SyncTest")
    older = fake.add_file("same.txt", b"stale cloud", parent=folder,
                          modified=_utc_stamp(50))
    newest = fake.add_file("same.txt", b"current cloud", parent=folder,
                           modified=_utc_stamp(30))
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        pair = SyncPair(str(root), "ElectriDrive/SyncTest",
                        direction="two_way", latest_wins=True)
        report = TwoWaySyncEngine(fake, db, pair).run()
        assert report.failed == 0
        assert report.downloaded == 1 and report.updated_duplicate_files == 1
        assert report.conflicts == 0 and not fake.uploads
        assert local.read_bytes() == b"current cloud"
        assert fake.nodes[older]["content"] == b"current cloud"
        assert fake.nodes[newest]["content"] == b"current cloud"
        assert [x[0] for x in fake.updates] == [older]
        # Old local data is recoverable, but not a second active sync file.
        assert list((root / ".electridrive-trash" / "overwritten").rglob("same.txt"))
        assert not list(root.glob("*conflict*"))
    finally:
        db.close()


def test_two_way_remote_newest_accepts_local_matching_older_cloud(tmp_path: Path):
    root = tmp_path / "docs"
    root.mkdir()
    local = root / "same.txt"
    local.write_bytes(b"same old content")
    _set_local_age(local, 55)
    fake = FakeDrive()
    folder = fake.ensure_folder_path("ElectriDrive/SyncTest")
    older = fake.add_file("same.txt", b"same old content",
                          parent=folder, modified=_utc_stamp(50))
    newest = fake.add_file("same.txt", b"new cloud content",
                           parent=folder, modified=_utc_stamp(30))
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        pair = SyncPair(str(root), "ElectriDrive/SyncTest",
                        direction="two_way", latest_wins=True)
        result = TwoWaySyncEngine(fake, db, pair).run()
        assert result.failed == 0 and result.downloaded == 1
        assert result.updated_duplicate_files == 1
        assert local.read_bytes() == b"new cloud content"
        assert fake.nodes[newest]["content"] == b"new cloud content"
        assert fake.nodes[older]["content"] == b"new cloud content"
        assert fake.updates[0][0] == older
        assert not fake.uploads
    finally:
        db.close()


def test_two_way_latest_wins_resolves_two_sided_changes_without_extra_copy():
    old = LastEntry("same.txt", "previous-local", "previous-remote", "id-same.txt")
    local_mtime = int((datetime.now(timezone.utc) - timedelta(days=2)).timestamp() * 1e9)
    local = LocalEntry("same.txt", local_mtime, 12, "local-changed")
    remote = RemoteEntry("same.txt", "id-same.txt", _utc_stamp(30),
                         "remote-changed", 15)
    actions = reconcile(
        {"same.txt": local}, {"same.txt": remote}, {"same.txt": old},
        latest_wins=True, direction="two_way",
    )
    assert len(actions) == 1 and actions[0].kind == ActionKind.UPLOAD

    newer_remote = RemoteEntry("same.txt", "id-same.txt", _utc_stamp(1),
                               "remote-changed", 15)
    actions = reconcile(
        {"same.txt": local}, {"same.txt": newer_remote}, {"same.txt": old},
        latest_wins=True, direction="two_way",
    )
    assert len(actions) == 1 and actions[0].kind == ActionKind.DOWNLOAD

    future = RemoteEntry("same.txt", "id-same.txt",
                         "2040-01-01T00:00:00Z", "remote-changed", 15)
    actions = reconcile(
        {"same.txt": local}, {"same.txt": future}, {"same.txt": old},
        latest_wins=True, direction="two_way",
    )
    assert len(actions) == 1 and actions[0].kind == ActionKind.CONFLICT


def test_two_way_latest_wins_remote_only_duplicates_download_verified_newest(tmp_path: Path):
    root = tmp_path / "new-downloads"
    root.mkdir()
    fake = FakeDrive()
    folder = fake.ensure_folder_path("ElectriDrive/SyncTest")
    older = fake.add_file("remote-only.txt", b"stale cloud",
                          parent=folder, modified=_utc_stamp(50))
    newest = fake.add_file("remote-only.txt", b"latest cloud",
                           parent=folder, modified=_utc_stamp(30))
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        pair = SyncPair(str(root), "ElectriDrive/SyncTest",
                        direction="two_way", latest_wins=True)
        result = TwoWaySyncEngine(fake, db, pair).run()
        assert result.failed == 0 and result.downloaded == 1
        assert result.auto_selected_duplicates == 1
        assert result.updated_duplicate_files == 1
        assert (root / "remote-only.txt").read_bytes() == b"latest cloud"
        assert fake.nodes[older]["content"] == b"latest cloud"
        assert fake.nodes[newest]["content"] == b"latest cloud"
        assert fake.updates[0][0] == older
        assert not fake.uploads
        assert not fake.nodes[older]["trashed"]
    finally:
        db.close()


def test_two_way_latest_wins_remote_only_tied_duplicates_block(tmp_path: Path):
    root = tmp_path / "new-downloads"
    root.mkdir()
    fake = FakeDrive()
    folder = fake.ensure_folder_path("ElectriDrive/SyncTest")
    older = fake.add_file("remote-only.txt", b"version A",
                          parent=folder, modified=_utc_stamp(30))
    newer = fake.add_file("remote-only.txt", b"version B",
                          parent=folder, modified=fake.nodes[older]["modified"])
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        pair = SyncPair(str(root), "ElectriDrive/SyncTest",
                        direction="two_way", latest_wins=True)
        result = TwoWaySyncEngine(fake, db, pair).run()
        assert result.failed >= 1 and result.blocked_before_transfers
        assert not (root / "remote-only.txt").exists()
        assert not fake.updates and not fake.uploads
        assert fake.nodes[older]["content"] == b"version A"
        assert fake.nodes[newer]["content"] == b"version B"
    finally:
        db.close()


def test_latest_wins_unique_cloud_file_overwrites_older_local(tmp_path: Path):
    root = tmp_path / "downloads"
    root.mkdir()
    local = root / "same.txt"
    local.write_bytes(b"stale local")
    _set_local_age(local, 30)
    fake = FakeDrive()
    folder = fake.ensure_folder_path("ElectriDrive/SyncTest")
    remote = fake.add_file("same.txt", b"current cloud",
                           parent=folder, modified=_utc_stamp(1))
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        # First adoption of a single existing remote object still needs an
        # explicit identity choice. Do not defeat the existing mapping guard.
        pair = SyncPair(str(root), "ElectriDrive/SyncTest",
                        direction="two_way", latest_wins=True,
                        canonical_remote_ids={"same.txt": remote})
        result = TwoWaySyncEngine(fake, db, pair).run()
        assert result.failed == 0 and result.downloaded == 1
        assert local.read_bytes() == b"current cloud"
        assert not fake.updates and not fake.uploads
        assert not list(root.glob("*conflict*"))
    finally:
        db.close()


def test_latest_wins_unique_existing_remote_without_history_updates_in_place(tmp_path: Path):
    root = tmp_path / "downloads"
    root.mkdir()
    local = root / "same.txt"
    local.write_bytes(b"latest local")
    _set_local_age(local, 1)
    fake = FakeDrive()
    folder = fake.ensure_folder_path("ElectriDrive/SyncTest")
    remote = fake.add_file("same.txt", b"older cloud",
                           parent=folder, modified=_utc_stamp(30))
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        pair = SyncPair(str(root), "ElectriDrive/SyncTest",
                        direction="two_way", latest_wins=True)
        result = TwoWaySyncEngine(fake, db, pair).run()
        assert result.failed == 0 and result.uploaded == 1
        assert fake.updates == [(remote, str(local), None)]
        assert not fake.uploads and result.conflicts == 0
        assert fake.nodes[remote]["content"] == b"latest local"
    finally:
        db.close()


def test_latest_wins_unique_cloud_newer_downloads_without_history(tmp_path: Path):
    root = tmp_path / "downloads"
    root.mkdir()
    local = root / "same.txt"
    local.write_bytes(b"older local")
    _set_local_age(local, 30)
    fake = FakeDrive()
    folder = fake.ensure_folder_path("ElectriDrive/SyncTest")
    remote = fake.add_file("same.txt", b"latest remote",
                           parent=folder, modified=_utc_stamp(1))
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        pair = SyncPair(str(root), "ElectriDrive/SyncTest",
                        direction="two_way", latest_wins=True)
        result = TwoWaySyncEngine(fake, db, pair).run()
        assert result.failed == 0 and result.downloaded == 1
        assert not fake.updates and not fake.uploads
        assert local.read_bytes() == b"latest remote"
        assert not list(root.glob("*conflict*"))
    finally:
        db.close()


def test_latest_wins_ambiguous_timestamps_fail_without_touching_files(tmp_path: Path):
    root = tmp_path / "downloads"
    root.mkdir()
    local = root / "same.txt"
    local.write_bytes(b"local")
    stamp = datetime.now(timezone.utc).timestamp() - 100
    os.utime(local, (stamp, stamp))
    fake = FakeDrive()
    folder = fake.ensure_folder_path("ElectriDrive/SyncTest")
    remote = fake.add_file("same.txt", b"cloud", parent=folder,
                           modified=datetime.fromtimestamp(stamp, timezone.utc).isoformat())
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        pair = SyncPair(str(root), "ElectriDrive/SyncTest",
                        direction="two_way", latest_wins=True,
                        canonical_remote_ids={"same.txt": remote})
        result = TwoWaySyncEngine(fake, db, pair).run()
        assert result.failed and result.blocked_before_transfers
        assert local.read_bytes() == b"local"
        assert not fake.updates and not fake.uploads
        assert not list(root.glob("*conflict*"))
    finally:
        db.close()


@pytest.mark.parametrize("local_newer", [True, False])
def test_latest_wins_blocks_parallel_cloud_edit_for_known_id(
    tmp_path: Path, local_newer: bool,
):
    root = tmp_path / "docs"
    root.mkdir()
    local = root / "same.txt"
    local.write_bytes(b"local content")
    _set_local_age(local, 1 if local_newer else 50)
    fake = FakeDrive()
    folder = fake.ensure_folder_path("ElectriDrive/SyncTest")
    remote = fake.add_file(
        "same.txt", b"cloud content", parent=folder,
        modified=_utc_stamp(30 if local_newer else 1),
    )
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        pair = SyncPair(
            str(root), "ElectriDrive/SyncTest",
            direction="two_way", latest_wins=True,
            canonical_remote_ids={"same.txt": remote},
        )
        engine = TwoWaySyncEngine(fake, db, pair)
        preflight = engine._verify_latest_pair_before_write

        def concurrent_remote_edit(rel, local_entry, remote_entry):
            fake.modify_file(remote, b"concurrent cloud edit", modified=_utc_stamp(0))
            preflight(rel, local_entry, remote_entry)

        engine._verify_latest_pair_before_write = concurrent_remote_edit
        result = engine.run()
        assert result.failed >= 1
        assert result.uploaded == result.downloaded == 0
        assert not fake.updates and not fake.uploads
        assert fake.nodes[remote]["content"] == b"concurrent cloud edit"
        assert local.read_bytes() == b"local content"
    finally:
        db.close()


def test_latest_wins_opt_out_keeps_legacy_conflict_copies(tmp_path: Path):
    root = tmp_path / "downloads"
    root.mkdir()
    local = root / "same.txt"
    local.write_bytes(b"local-new")
    _set_local_age(local, 1)
    fake = FakeDrive()
    folder = fake.ensure_folder_path("ElectriDrive/SyncTest")
    remote = fake.add_file("same.txt", b"remote-old", parent=folder,
                           modified=_utc_stamp(30))
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        pair = SyncPair(str(root), "ElectriDrive/SyncTest",
                        direction="two_way", latest_wins=False,
                        canonical_remote_ids={"same.txt": remote})
        report = TwoWaySyncEngine(fake, db, pair).run()
        assert report.failed == 0 and report.conflicts == 1
        assert list(root.glob("*conflict*"))
    finally:
        db.close()


def test_up_only_uses_recorded_remote_id_when_duplicates_appear(tmp_path: Path):
    root = tmp_path / "uploads"
    root.mkdir()
    local_file = root / "target.txt"
    local_file.write_bytes(b"base")
    fake = FakeDrive()
    folder = fake.ensure_folder_path("ElectriDrive/SyncTest")
    canonical = fake.add_file("target.txt", b"base", parent=folder)
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        pair = SyncPair(str(root), "ElectriDrive/SyncTest", direction="up_only")
        engine = TwoWaySyncEngine(fake, db, pair)
        initial = engine.run()
        assert initial.failed == 0
        assert db.get_sync_item(engine.pair_id, "target.txt").remote_id == canonical
        duplicate = fake.add_file("target.txt", b"unrelated version", parent=folder)
        local_file.write_bytes(b"fresh local")
        report = engine.run()
        assert report.failed == 0 and report.uploaded == 1
        assert fake.nodes[canonical]["content"] == b"fresh local"
        assert fake.nodes[duplicate]["content"] == b"unrelated version"
        assert len(fake.updates) == 1
        assert fake.updates[0][0] == canonical
        assert not fake.nodes[duplicate]["trashed"]
    finally:
        db.close()


def test_up_only_rechecks_cached_duplicate_paths_when_local_file_appears(
    tmp_path: Path,
):
    root = tmp_path / "uploads"
    root.mkdir()
    fake = FakeDrive()
    folder = fake.ensure_folder_path("ElectriDrive/SyncTest")
    fake.add_file("now-local.txt", b"a", parent=folder)
    fake.add_file("now-local.txt", b"b", parent=folder)
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        pair = SyncPair(str(root), "ElectriDrive/SyncTest", direction="up_only")
        engine = TwoWaySyncEngine(fake, db, pair)
        assert engine.run().failed == 0
        # There are no Drive changes; _remote_for_run reuses an in-memory tree.
        (root / "now-local.txt").write_bytes(b"new data")
        second = engine.run()
        assert second.failed >= 1
        assert any("ambiguous upload target" in e for e in second.errors)
        assert not fake.uploads and not fake.updates
    finally:
        db.close()


def test_two_way_keeps_fail_closed_on_identical_remote_paths(
    tmp_path: Path, caplog,
):
    root = tmp_path / "sync"
    root.mkdir()
    fake = FakeDrive()
    folder = fake.ensure_folder_path("ElectriDrive/SyncTest")
    first = fake.add_file("same.txt", b"a", parent=folder)
    second = fake.add_file("same.txt", b"b", parent=folder)
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        report = TwoWaySyncEngine(fake, db, _pair(root)).run()
        assert report.failed >= 1
        assert any("duplicate remote file path" in e for e in report.errors)
        assert len(report.remote_path_conflicts) == 1
        conflict = report.remote_path_conflicts[0]
        assert conflict.remote_path == "same.txt"
        assert conflict.direction == "two_way"
        assert {candidate.file_id for candidate in conflict.candidates} == {
            first, second,
        }
        assert not conflict.resolved
        assert all("same.txt" not in error for error in report.errors)
        assert "same.txt" not in caplog.text
        assert not fake.uploads and not fake.updates
        assert not (root / "same.txt").exists()
    finally:
        db.close()


def test_duplicate_candidates_split_across_drive_pages_remain_blocked(tmp_path: Path):
    from electridrive.google_api.client import FileListing

    class PagedFakeDrive(FakeDrive):
        page_parent = ""

        def list_folder(self, parent_id="root", page_token=None):
            listing = super().list_folder(parent_id, page_token)
            if parent_id != self.page_parent:
                return listing
            if page_token is None:
                return FileListing(listing.files[:1], "next")
            return FileListing(listing.files[1:], None)

    root = tmp_path / "sync"
    root.mkdir()
    fake = PagedFakeDrive()
    folder = fake.ensure_folder_path("ElectriDrive/SyncTest")
    fake.page_parent = folder
    first = fake.add_file("same.txt", b"first", parent=folder)
    second = fake.add_file("same.txt", b"second", parent=folder)
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        report = TwoWaySyncEngine(fake, db, _pair(root)).run()
        assert report.failed == 1
        assert {c.file_id for c in report.remote_path_conflicts[0].candidates} == {
            first, second,
        }
        assert not fake.uploads and not fake.updates
        assert not fake.nodes[first]["trashed"] and not fake.nodes[second]["trashed"]
    finally:
        db.close()


def test_owner_selected_canonical_id_survives_settings_reload(tmp_path: Path):
    root = tmp_path / "sync"
    root.mkdir()
    (root / "same.txt").write_bytes(b"selected")
    fake = FakeDrive()
    folder = fake.ensure_folder_path("ElectriDrive/SyncTest")
    first_duplicate = fake.add_file("same.txt", b"first", parent=folder)
    second_duplicate = fake.add_file("same.txt", b"second", parent=folder)
    canonical = fake.add_file("same.txt", b"selected", parent=folder)
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        pair = SyncPair(
            str(root), "ElectriDrive/SyncTest",
            canonical_remote_ids={"same.txt": canonical},
        )
        persisted = Settings.from_dict(Settings(sync_pairs=[pair]).to_dict())
        reloaded_pair = persisted.sync_pairs[0]
        first = TwoWaySyncEngine(fake, db, reloaded_pair).run()
        assert first.failed == 0
        assert first.remote_path_conflicts[0].resolved
        assert first.remote_path_conflicts[0].canonical_id == canonical
        assert {
            candidate.file_id
            for candidate in first.remote_path_conflicts[0].candidates
        } == {first_duplicate, second_duplicate, canonical}

        restarted_pair = Settings.from_dict(persisted.to_dict()).sync_pairs[0]
        second = TwoWaySyncEngine(fake, db, restarted_pair).run()
        assert second.failed == 0
        assert second.uploaded == second.downloaded == 0
        assert second.remote_path_conflicts[0].resolved
        assert fake.nodes[canonical]["content"] == b"selected"
        assert fake.nodes[first_duplicate]["content"] == b"first"
        assert fake.nodes[second_duplicate]["content"] == b"second"
        assert not fake.nodes[canonical]["trashed"]
        assert not fake.nodes[first_duplicate]["trashed"]
        assert not fake.nodes[second_duplicate]["trashed"]
        assert not fake.updates and not fake.uploads
    finally:
        db.close()


def test_reviewed_canonical_id_only_updates_selected_duplicate_after_reload(
    tmp_path: Path,
):
    root = tmp_path / "sync"
    root.mkdir()
    local_file = root / "same.txt"
    local_file.write_bytes(b"selected")
    fake = FakeDrive()
    folder = fake.ensure_folder_path("ElectriDrive/SyncTest")
    first_duplicate = fake.add_file("same.txt", b"first", parent=folder)
    second_duplicate = fake.add_file("same.txt", b"second", parent=folder)
    canonical = fake.add_file("same.txt", b"selected", parent=folder)
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        pair = SyncPair(
            str(root), "ElectriDrive/SyncTest", direction="up_only",
            delete_policy="off", canonical_remote_ids={"same.txt": canonical},
        )
        persisted = Settings.from_dict(Settings(sync_pairs=[pair]).to_dict())
        initial = TwoWaySyncEngine(fake, db, persisted.sync_pairs[0]).run()
        assert initial.failed == 0
        assert initial.uploaded == initial.downloaded == 0

        local_file.write_bytes(b"new selected content")
        restarted_pair = Settings.from_dict(persisted.to_dict()).sync_pairs[0]
        report = TwoWaySyncEngine(fake, db, restarted_pair).run()

        assert report.failed == 0 and report.uploaded == 1
        assert report.downloaded == report.trashed_local == report.trashed_remote == 0
        assert fake.updates == [(canonical, str(local_file), None)]
        assert fake.nodes[canonical]["content"] == b"new selected content"
        assert fake.nodes[first_duplicate]["content"] == b"first"
        assert fake.nodes[second_duplicate]["content"] == b"second"
        assert not fake.nodes[first_duplicate]["trashed"]
        assert not fake.nodes[second_duplicate]["trashed"]
    finally:
        db.close()


def test_duplicate_folder_and_file_folder_collisions_are_reported_without_mutation(
    tmp_path: Path,
):
    root = tmp_path / "sync"
    root.mkdir()
    fake = FakeDrive()
    folder = fake.ensure_folder_path("ElectriDrive/SyncTest")
    fake.add_folder("duplicate", parent=folder)
    fake.add_folder("duplicate", parent=folder)
    fake.add_file("also-folder", b"file", parent=folder)
    fake.add_folder("also-folder", parent=folder)
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        report = TwoWaySyncEngine(fake, db, _pair(root)).run()
        assert report.failed >= 1
        by_path = {item.remote_path: item for item in report.remote_path_conflicts}
        assert len(by_path["duplicate"].candidates) == 2
        assert len(by_path["also-folder"].candidates) == 2
        assert not fake.uploads and not fake.updates
        assert all(not node["trashed"] for node in fake.nodes.values())
        assert not (root / "duplicate").exists()
        assert not (root / "also-folder").exists()
    finally:
        db.close()


def test_full_backup_upload_only_includes_hidden_credentials_build_and_temp_files(
    tmp_path: Path,
):
    """All regular files in the explicitly selected root, including dotfiles."""
    root = tmp_path / "selected-root"
    root.mkdir()
    contents = {
        ".env": b"test-token-placeholder",
        ".git/config": b"test-git-config",
        ".venv/bin/python": b"test-venv-file",
        "node_modules/lib/index.js": b"test-js",
        "Unicode äöü/雪 file.txt": b"unicode-file",
        "zero-byte.bin": b"",
        "large.bin": b"x" * (1024 * 1024),
        "secret.tmp": b"test-temporary-file",
        ".electridrive-trash/old.bak~": b"test-trash-copy",
        ".hidden/credentials.json": b"test-oauth-placeholder",
        "regular.txt": b"test-regular",
    }
    for name, data in contents.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (root / "empty directory" / "nested empty").mkdir(parents=True)
    fake = FakeDrive()
    db = SyncDatabase(tmp_path / "state.sqlite3")
    pair = SyncPair(
        str(root), "ElectriDrive/Test-Complete-Backup",
        direction="up_only", delete_policy="off",
    )
    try:
        assert pair.include_all_files
        engine = TwoWaySyncEngine(fake, db, pair)
        result = engine.run()
        assert result.failed == result.unexplained_omissions == 0
        assert result.excluded_local == 0
        assert result.uploaded == len(contents)
        uploaded_contents = {
            remote_name: fake.nodes[file_id]["content"]
            for _local_path, _parent_id, remote_name, file_id in fake.uploads
        }
        assert uploaded_contents == {
            Path(path).name: data for path, data in contents.items()
        }
        remote_root = fake.ensure_folder_path("ElectriDrive/Test-Complete-Backup")
        empty_directory = fake.find_folder("empty directory", remote_root)
        assert empty_directory is not None
        assert fake.find_folder("nested empty", empty_directory) is not None
        remote_names = {
            n["name"]
            for n in fake.nodes.values()
            if n["mime"] != "application/vnd.google-apps.folder"
            and not n["trashed"]
        }
        assert remote_names == {Path(p).name for p in contents}
        assert all(not n["trashed"] for n in fake.nodes.values())

        upload_count = len(fake.uploads)
        update_count = len(fake.updates)
        unchanged = engine.run()
        assert unchanged.failed == unchanged.unexplained_omissions == 0
        assert unchanged.uploaded == 0
        assert len(fake.uploads) == upload_count
        assert len(fake.updates) == update_count
    finally:
        db.close()


def test_full_backup_rejects_unsupported_symlink_without_drive_mutation(
    tmp_path: Path,
):
    root = tmp_path / "selected-root"
    root.mkdir()
    (root / "regular.txt").write_bytes(b"safe")
    (root / "internal-relative").symlink_to(Path("regular.txt"))
    outside = tmp_path / "outside-python"
    outside.write_bytes(b"must remain outside the selected root")
    (root / "external-absolute").symlink_to(outside)
    venv_python = root / ".venv/bin/python"
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to(outside)
    (root / "dangling-relative").symlink_to(Path("missing"))
    (root / "cycle-a").symlink_to(Path("cycle-b"), target_is_directory=True)
    (root / "cycle-b").symlink_to(Path("cycle-a"), target_is_directory=True)
    fake = FakeDrive()
    db = SyncDatabase(tmp_path / "state.sqlite3")
    pair = SyncPair(str(root), "ElectriDrive/Test-No-Silent-Skips",
                    direction="up_only", delete_policy="off")
    try:
        report = TwoWaySyncEngine(fake, db, pair).run()
        assert report.failed >= 1
        assert all(
            any(f"release blocker: unsupported symlink {path}" in err
                for err in report.errors)
            for path in (
                "internal-relative",
                "external-absolute",
                ".venv/bin/python",
                "dangling-relative",
                "cycle-a",
                "cycle-b",
            )
        )
        assert not fake.uploads and not fake.updates
        assert len(fake.nodes) == 1  # no remote folder creation
    finally:
        db.close()


def test_full_backup_can_be_explicitly_disabled_for_legacy_filtered_sync(
    tmp_path: Path,
):
    root = tmp_path / "selected-root"
    root.mkdir()
    (root / ".secret").write_bytes(b"private")
    (root / "plain.txt").write_bytes(b"plain")
    fake = FakeDrive()
    db = SyncDatabase(tmp_path / "state.sqlite3")
    pair = SyncPair(str(root), "ElectriDrive/Test-Legacy-Filtered",
                    direction="up_only", include_all_files=False)
    try:
        result = TwoWaySyncEngine(fake, db, pair).run()
        assert result.failed == 0
        assert result.excluded_local == 1
        assert result.uploaded == 1
    finally:
        db.close()

@pytest.mark.parametrize("direction", ["up_only", "two_way", "down_only"])
def test_reviewed_drive_id_removed_blocks_surviving_duplicate_without_writes(
    tmp_path: Path, direction: str,
):
    root = tmp_path / "sync"
    root.mkdir()
    local = root / "same.txt"
    local.write_bytes(b"chosen original")
    fake = FakeDrive()
    folder = fake.ensure_folder_path("ElectriDrive/SyncTest")
    chosen = fake.add_file("same.txt", b"chosen original", parent=folder)
    untouched = fake.add_file("same.txt", b"unselected original", parent=folder)
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        pair = SyncPair(str(root), "ElectriDrive/SyncTest",
                        direction=direction, delete_policy="off",
                        canonical_remote_ids={"same.txt": chosen})
        engine = TwoWaySyncEngine(fake, db, pair)
        assert engine.run().failed == 0
        fake.trash(chosen)
        local.write_bytes(b"changed locally")
        updates_before = len(fake.updates)
        uploads_before = len(fake.uploads)
        result = engine.run()
        assert result.failed >= 1
        assert result.uploaded == result.downloaded == 0
        assert result.trashed_local == result.trashed_remote == 0
        assert len(fake.updates) == updates_before
        assert len(fake.uploads) == uploads_before
        assert fake.nodes[untouched]["content"] == b"unselected original"
        assert local.read_bytes() == b"changed locally"
        assert any(c.remote_path == "same.txt" and not c.resolved
                   for c in result.remote_path_conflicts)
    finally:
        db.close()


@pytest.mark.parametrize("direction", ["up_only", "two_way", "down_only"])
def test_reviewed_drive_id_missing_entirely_blocks_before_sync(
    tmp_path: Path, direction: str,
):
    root = tmp_path / "sync"
    root.mkdir()
    local = root / "same.txt"
    local.write_bytes(b"original")
    fake = FakeDrive()
    folder = fake.ensure_folder_path("ElectriDrive/SyncTest")
    chosen = fake.add_file("same.txt", b"original", parent=folder)
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        pair = SyncPair(str(root), "ElectriDrive/SyncTest",
                        direction=direction, delete_policy="off",
                        canonical_remote_ids={"same.txt": chosen})
        engine = TwoWaySyncEngine(fake, db, pair)
        assert engine.run().failed == 0
        fake.trash(chosen)
        local.write_bytes(b"changed locally")
        result = engine.run()
        assert result.failed >= 1
        assert result.uploaded == result.downloaded == 0
        assert not fake.uploads and not fake.updates
        assert local.read_bytes() == b"changed locally"
        assert any(c.remote_path == "same.txt" and not c.candidates
                   for c in result.remote_path_conflicts)
    finally:
        db.close()


def test_reviewed_native_workspace_file_not_binary_upload_target(tmp_path: Path):
    root = tmp_path / "sync"
    root.mkdir()
    (root / "Notes.docx").write_bytes(b"binary local")
    fake = FakeDrive()
    folder = fake.ensure_folder_path("ElectriDrive/SyncTest")
    native = fake.add_file("Notes", b"native original", parent=folder,
                           mime="application/vnd.google-apps.document")
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        pair = SyncPair(str(root), "ElectriDrive/SyncTest", direction="up_only",
                        delete_policy="off",
                        canonical_remote_ids={"Notes.docx": native})
        report = TwoWaySyncEngine(fake, db, pair).run()
        assert report.failed >= 1
        assert report.uploaded == report.downloaded == 0
        assert not fake.uploads and not fake.updates
        assert fake.nodes[native]["content"] == b"native original"
    finally:
        db.close()


def test_download_only_workspace_conflict_keeps_edits_without_cloud_writes(tmp_path: Path):
    root = tmp_path / "sync"
    root.mkdir()
    local = root / "Notes.docx"
    local.write_bytes(b"my edited export")
    fake = FakeDrive()
    folder = fake.ensure_folder_path("QA")
    native = fake.add_file("Notes", b"native original", parent=folder,
                           mime="application/vnd.google-apps.document")
    original_nodes = {key: dict(value) for key, value in fake.nodes.items()}
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        pair = SyncPair(str(root), "QA", direction="down_only", delete_policy="off")
        engine = TwoWaySyncEngine(fake, db, pair)
        report = engine.run()
        assert report.failed == 0
        assert local.read_bytes() == b"EXPORTED:native original"
        copies = list(root.glob("Notes (conflict *).docx"))
        assert len(copies) == 1 and copies[0].read_bytes() == b"my edited export"
        assert report.uploaded == report.trashed_remote == report.trashed_local == 0
        assert fake.nodes == original_nodes
        assert not fake.uploads and not fake.updates
        # A repeat must keep the local preservation copy out of remote authority.
        assert engine.run().failed == 0
        assert copies[0].read_bytes() == b"my edited export"
        assert fake.nodes[native]["content"] == b"native original"
    finally:
        db.close()


def test_remote_pagination_cycle_stops_before_any_transfer(tmp_path: Path):
    from electridrive.google_api.client import FileListing

    class CyclingDrive(FakeDrive):
        get_start_page_token = None
        list_changes = None
        listing_calls = 0

        def list_folder(self, parent_id="root", page_token=None):
            self.listing_calls += 1
            if self.listing_calls > 3:
                raise RuntimeError("test watchdog: infinite pagination")
            return FileListing([], "again")

    root = tmp_path / "sync"
    root.mkdir()
    (root / "local.txt").write_bytes(b"preserve")
    fake = CyclingDrive()
    fake.ensure_folder_path("QA")
    from contextlib import closing
    with closing(SyncDatabase(tmp_path / "state.sqlite3")) as db:
        report = TwoWaySyncEngine(fake, db, SyncPair(str(root), "QA")).run()
    assert report.failed >= 1
    assert any("repeated page token" in error for error in report.errors)
    assert not fake.uploads and not fake.updates
    assert (root / "local.txt").read_bytes() == b"preserve"


def test_changes_feed_detects_multi_page_cycle(tmp_path: Path):
    class CyclingDrive(FakeDrive):
        calls = 0

        def list_changes(self, page_token):
            self.calls += 1
            if self.calls > 4:
                raise RuntimeError("test watchdog: infinite pagination")
            return [], {"start": "a", "a": "b", "b": "a"}[page_token], None

    fake = CyclingDrive()
    from contextlib import closing
    with closing(SyncDatabase(tmp_path / "state.sqlite3")) as db:
        engine = TwoWaySyncEngine(fake, db, SyncPair(str(tmp_path), "QA"))
        with pytest.raises(RuntimeError, match="repeated page token"):
            engine._consume_changes("start")


def test_download_only_missing_source_does_not_create_drive_folders(tmp_path: Path):
    from contextlib import closing

    root = tmp_path / "sync"
    root.mkdir()
    (root / "keep.txt").write_bytes(b"local original")
    fake = FakeDrive()
    with closing(SyncDatabase(tmp_path / "state.sqlite3")) as db:
        pair = SyncPair(str(root), "QA/Missing", direction="down_only")
        report = TwoWaySyncEngine(fake, db, pair).run()
    assert report.failed >= 1
    assert set(fake.nodes) == {"root"}
    assert (root / "keep.txt").read_bytes() == b"local original"


def test_four_ambiguous_paths_count_checks_but_log_one_summary(tmp_path, caplog):
    from copy import deepcopy
    from contextlib import closing

    root = tmp_path / "synthetic"
    root.mkdir()
    fake = FakeDrive()
    folder = fake.ensure_folder_path("QA")
    names = ("alpha.txt", "beta.txt", "gamma.txt", "delta.txt")
    for name in names:
        (root / name).write_bytes(b"local synthetic")
        for index in range(3):
            fake.add_file(name, str(index).encode(), parent=folder)
    before = deepcopy(fake.nodes)
    with closing(SyncDatabase(tmp_path / "state.sqlite3")) as db:
        report = TwoWaySyncEngine(fake, db, SyncPair(
            str(root), "QA", direction="up_only", delete_policy="off",
        )).run()
    assert report.failed == 4  # paths, not the twelve candidate IDs or retries
    assert len(report.errors) == 1
    assert report.blocked_before_transfers
    records = [r for r in caplog.records if "ambiguous upload target" in r.getMessage()]
    assert len(records) == 1
    assert "4" in records[0].getMessage()
    assert {c.remote_path for c in report.remote_path_conflicts if c.blocks_sync} == set(names)
    assert all(len(c.candidates) == 3 for c in report.remote_path_conflicts)
    assert report.uploaded == report.downloaded == report.trashed_remote == report.trashed_local == 0
    assert fake.nodes == before


def test_remote_only_duplicates_are_not_offered_as_blocking_upload_targets(tmp_path):
    from contextlib import closing

    root = tmp_path / "synthetic"
    root.mkdir()
    (root / "target.txt").write_bytes(b"local synthetic")
    fake = FakeDrive()
    folder = fake.ensure_folder_path("QA")
    for name in ("target.txt", "remote-only.txt"):
        fake.add_file(name, b"first", parent=folder)
        fake.add_file(name, b"second", parent=folder)
    with closing(SyncDatabase(tmp_path / "state.sqlite3")) as db:
        report = TwoWaySyncEngine(fake, db, SyncPair(
            str(root), "QA", direction="up_only",
        )).run()
    assert report.failed == 1
    blockers = {c.remote_path for c in report.remote_path_conflicts
                if getattr(c, "blocks_sync", True)}
    assert blockers == {"target.txt"}
    assert not fake.uploads and not fake.updates


def test_failed_root_rescan_does_not_offer_old_duplicate_candidates(tmp_path):
    from contextlib import closing

    class UnavailableDrive(FakeDrive):
        unavailable = False

        def get_metadata(self, file_id):
            if self.unavailable:
                raise RuntimeError("synthetic root unavailable")
            return super().get_metadata(file_id)

    root = tmp_path / "synthetic"
    root.mkdir()
    (root / "target.txt").write_bytes(b"local synthetic")
    fake = UnavailableDrive()
    folder = fake.ensure_folder_path("QA")
    fake.add_file("target.txt", b"first", parent=folder)
    fake.add_file("target.txt", b"second", parent=folder)
    with closing(SyncDatabase(tmp_path / "state.sqlite3")) as db:
        engine = TwoWaySyncEngine(fake, db, SyncPair(str(root), "QA", direction="up_only"))
        assert engine.run().remote_path_conflicts
        fake.unavailable = True
        report = engine.run()
    assert report.failed == 1
    assert report.remote_path_conflicts == []
    assert not fake.uploads and not fake.updates
