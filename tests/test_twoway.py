from pathlib import Path

import pytest

from electridrive.config import SyncPair
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
