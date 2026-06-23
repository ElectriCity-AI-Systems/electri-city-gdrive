from pathlib import Path

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
