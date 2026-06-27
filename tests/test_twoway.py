from pathlib import Path

from electridrive.config import SyncPair
from electridrive.storage.database import SyncDatabase
from electridrive.sync.twoway import (
    Action,
    ActionKind,
    LastEntry,
    LocalEntry,
    RemoteEntry,
    SyncReport,
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


def test_upload_action_skips_file_removed_after_scan(tmp_path: Path):
    root = tmp_path / "sync"
    root.mkdir()

    fake = FakeDrive()
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        engine = TwoWaySyncEngine(fake, db, _pair(root))
        engine._dir_ids = {"": fake.ensure_folder_path("ElectriDrive/SyncTest")}
        local = {"vanished.txt": L("vanished.txt", "old")}
        report = SyncReport()

        engine._apply(Action(ActionKind.UPLOAD, "vanished.txt"), local, {}, report)

        assert report.skipped == 1
        assert report.failed == 0
        assert fake.uploads == []
    finally:
        db.close()


def test_scan_local_counts_file_changed_while_hashing(tmp_path: Path, monkeypatch):
    root = tmp_path / "sync"
    root.mkdir()
    (root / "live.txt").write_text("old", encoding="utf-8")

    def mutate_while_hashing(path):
        Path(path).write_text("changed while hashing", encoding="utf-8")
        return "digest"

    monkeypatch.setattr("electridrive.sync.twoway.md5_file", mutate_while_hashing)
    fake = FakeDrive()
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        engine = TwoWaySyncEngine(fake, db, _pair(root))
        report = SyncReport()

        assert engine.scan_local(report) == {}
        assert report.skipped == 1
    finally:
        db.close()


# ------------------------------------------------- Phase 1/2: local hash-skip
def test_scan_local_reuses_md5_when_mtime_size_unchanged(tmp_path: Path, monkeypatch):
    root = tmp_path / "sync"
    root.mkdir()
    f = root / "a.txt"
    f.write_text("alpha", encoding="utf-8")
    fake = FakeDrive()
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        engine = TwoWaySyncEngine(fake, db, _pair(root))
        st = f.stat()
        baseline = {"a.txt": (st.st_mtime_ns, st.st_size, "cached-md5")}

        def boom(_path):
            raise AssertionError("an unchanged file must not be re-hashed")

        monkeypatch.setattr("electridrive.sync.twoway.md5_file", boom)
        out = engine.scan_local(baseline=baseline)
        assert out["a.txt"].md5 == "cached-md5"
        assert out["a.txt"].mtime_ns == st.st_mtime_ns
    finally:
        db.close()


def test_scan_local_rehashes_when_mtime_size_differ(tmp_path: Path, monkeypatch):
    root = tmp_path / "sync"
    root.mkdir()
    (root / "a.txt").write_text("alpha", encoding="utf-8")
    fake = FakeDrive()
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        engine = TwoWaySyncEngine(fake, db, _pair(root))
        baseline = {"a.txt": (123, 999, "stale-md5")}  # won't match -> must hash
        monkeypatch.setattr("electridrive.sync.twoway.md5_file", lambda _p: "fresh-md5")
        out = engine.scan_local(baseline=baseline)
        assert out["a.txt"].md5 == "fresh-md5"
    finally:
        db.close()


def test_parallel_hashing_counts_all_racing_files(tmp_path: Path, monkeypatch):
    root = tmp_path / "sync"
    root.mkdir()
    for i in range(5):
        (root / f"f{i}.txt").write_text("x", encoding="utf-8")

    def mutate_while_hashing(path):
        Path(path).write_text("changed-while-hashing", encoding="utf-8")
        return "digest"

    monkeypatch.setattr("electridrive.sync.twoway.md5_file", mutate_while_hashing)
    fake = FakeDrive()
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        engine = TwoWaySyncEngine(fake, db, _pair(root))
        report = SyncReport()
        out = engine.scan_local(report)  # no baseline -> all five go through the pool
        assert out == {}
        assert report.skipped == 5  # tallied in the main thread, not by workers
        assert report.hashed == 0
    finally:
        db.close()


# --------------------------------------- Phase 3: change-token incremental path
def test_first_sync_seeds_token_and_cache(tmp_path: Path):
    root = tmp_path / "sync"
    root.mkdir()
    (root / "a.txt").write_text("alpha", encoding="utf-8")
    fake = FakeDrive()
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        engine = TwoWaySyncEngine(fake, db, _pair(root))
        report = engine.run()
        assert report.uploaded == 1
        assert report.full_remote_scan is True
        assert db.get_change_token(engine._get_account_key())  # token persisted

        # The upload happened after the seed walk, so it lands in the cache on the
        # next drain. A 2nd sync folds it in and is fully incremental.
        before = fake.list_folder_calls
        report2 = engine.run()
        assert report2.full_remote_scan is False
        assert fake.list_folder_calls == before  # no remote walk on the fast path
        remote_root = fake.ensure_folder_path("ElectriDrive/SyncTest")
        rows = {r["name"]: r for r in db.get_cached_children(remote_root)}
        assert "a.txt" in rows and rows["a.txt"]["md5"]
    finally:
        db.close()


def test_cache_path_matches_full_scan_and_avoids_list_folder(tmp_path: Path):
    root = tmp_path / "sync"
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_text("alpha", encoding="utf-8")
    (root / "sub" / "b.txt").write_text("beta", encoding="utf-8")
    fake = FakeDrive()
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        engine = TwoWaySyncEngine(fake, db, _pair(root))
        engine.run()  # initial sync seeds cache + token

        remote_root = fake.ensure_folder_path("ElectriDrive/SyncTest")
        fake.add_file("c.txt", b"gamma", parent=remote_root)         # add file
        a_id = next(i for i, n in fake.nodes.items() if n["name"] == "a.txt")
        fake.nodes[a_id]["content"] = b"ALPHA-EDITED"                 # modify file
        fake._record_change(a_id)
        docs = fake.add_folder("docs", parent=remote_root)           # add folder
        fake.add_file("d.txt", b"delta", parent=docs)                # file in new folder

        last, baseline = engine._load_state()
        local = engine.scan_local(baseline=baseline)

        before = fake.list_folder_calls
        remote_cache, full_scanned = engine._acquire_remote()
        assert full_scanned is False
        assert fake.list_folder_calls == before  # cache path issued ZERO list_folder

        remote_full = engine.scan_remote()  # authoritative live walk

        assert kinds(reconcile(local, remote_cache, last)) == \
            kinds(reconcile(local, remote_full, last))
    finally:
        db.close()


def test_guard2_stale_cache_never_trashes_local(tmp_path: Path):
    root = tmp_path / "sync"
    root.mkdir()
    (root / "a.txt").write_text("alpha", encoding="utf-8")
    fake = FakeDrive()
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        engine = TwoWaySyncEngine(fake, db, _pair(root))
        engine.run()  # a.txt synced both sides; token + cache seeded

        # Make the cache falsely empty while pinning the token to the CURRENT
        # high-water, so the drain finds nothing to replay and the incremental path
        # genuinely sees an empty remote (the worst case GUARD 2 must catch).
        with db._lock:
            db._conn.execute("DELETE FROM remote_nodes")
            db._conn.commit()
        db.set_change_token(engine._get_account_key(), fake.get_start_page_token())

        before_lf = fake.list_folder_calls
        report = engine.run()
        assert report.trashed_local == 0  # GUARD 2 prevented the false deletion
        assert (root / "a.txt").read_text(encoding="utf-8") == "alpha"
        assert report.full_remote_scan is True
        assert fake.list_folder_calls > before_lf  # a real re-walk happened
    finally:
        db.close()


def test_genuine_remote_deletion_trashes_local_through_guard2(tmp_path: Path):
    root = tmp_path / "sync"
    root.mkdir()
    (root / "a.txt").write_text("alpha", encoding="utf-8")
    fake = FakeDrive()
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        engine = TwoWaySyncEngine(fake, db, _pair(root))
        engine.run()  # a.txt synced both sides

        a_id = next(i for i, n in fake.nodes.items() if n["name"] == "a.txt")
        fake.trash(a_id)  # genuine remote deletion (emits a trashed change)

        report = engine.run()
        assert report.trashed_local == 1  # GUARD 2 confirmed it's really gone
        assert report.full_remote_scan is True
        assert not (root / "a.txt").exists()
        assert (root / ".electridrive-trash" / "a.txt").exists()
    finally:
        db.close()


def test_guard0_invalid_token_falls_back_to_full_scan(tmp_path: Path, monkeypatch):
    root = tmp_path / "sync"
    root.mkdir()
    (root / "a.txt").write_text("alpha", encoding="utf-8")
    fake = FakeDrive()
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        engine = TwoWaySyncEngine(fake, db, _pair(root))
        engine.run()  # seeds a token

        remote_root = fake.ensure_folder_path("ElectriDrive/SyncTest")
        fake.add_file("c.txt", b"gamma", parent=remote_root)  # a full scan must find this

        class _Resp:
            status = 410

        def boom(_token):
            err = Exception("invalid pageToken")
            err.resp = _Resp()
            raise err

        monkeypatch.setattr(fake, "list_changes", boom)

        report = engine.run()
        assert report.full_remote_scan is True  # GUARD 0 fell back to a full walk
        assert report.downloaded == 1
        assert report.failed == 0
        assert (root / "c.txt").read_text(encoding="utf-8") == "gamma"
    finally:
        db.close()
