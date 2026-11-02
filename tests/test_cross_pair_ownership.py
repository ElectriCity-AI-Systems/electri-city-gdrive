"""Regression tests for pair-scoped Drive-ID ownership.

A name/path match at the upload target is not ownership: only a remote_id a
sync pair has itself previously recorded may be trusted as something safe to
overwrite. These tests cover the gap found during the 2.2.0 release
acceptance pass (an unrelated pair's file could be silently overwritten by a
different pair uploading a same-named file) for both engines that perform
upload-style writes: UploadOnlySyncEngine (the `sync-up` CLI command) and
TwoWaySyncEngine in `up_only` direction (the `sync --direction up_only` CLI
command and the GUI's upload-only pairs).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from electridrive.config import SyncPair
from electridrive.storage.database import SyncDatabase
from electridrive.sync.engine import UploadOnlySyncEngine
from electridrive.sync.rules import SyncRules
from electridrive.sync.twoway import TwoWaySyncEngine
from fakes import FakeDrive


def _up_only_pair(local_root: Path, remote_folder: str) -> SyncPair:
    return SyncPair(str(local_root), remote_folder, direction="up_only", delete_policy="off")


# --------------------------------------------------------------- engine.py (sync-up)

def test_engine_first_sync_adopts_untracked_existing_file(tmp_path: Path) -> None:
    """Regression safety net: a brand-new pair may still adopt genuinely
    untracked ("wild") pre-existing Drive content on its first sync."""
    fake = FakeDrive()
    root = tmp_path / "local"
    root.mkdir()
    (root / "shared.txt").write_bytes(b"local-content")
    remote_root = fake.ensure_folder_path("ElectriDrive/Wild")
    wild_id = fake.add_file("shared.txt", b"pre-existing, never tracked by electridrive", parent=remote_root)

    db = SyncDatabase(tmp_path / "db.sqlite3")
    try:
        engine = UploadOnlySyncEngine(drive_client=fake, database=db, rules=SyncRules())
        result = engine.sync_up(root, "ElectriDrive/Wild")
    finally:
        db.close()

    assert result.failed == 0
    assert result.uploaded == 1
    assert fake.nodes[wild_id]["content"] == b"local-content"


def test_engine_separate_pair_refuses_to_overwrite_claimed_file(tmp_path: Path) -> None:
    fake = FakeDrive()
    root_a = tmp_path / "pair-a"
    root_a.mkdir()
    (root_a / "shared.txt").write_bytes(b"from-pair-a")
    root_b = tmp_path / "pair-b"
    root_b.mkdir()
    (root_b / "shared.txt").write_bytes(b"from-pair-b")
    (root_b / "unique-b.txt").write_bytes(b"unrelated file, no collision")

    db = SyncDatabase(tmp_path / "db.sqlite3")
    try:
        engine_a = UploadOnlySyncEngine(drive_client=fake, database=db, rules=SyncRules())
        first = engine_a.sync_up(root_a, "ElectriDrive/Shared")
        assert first.failed == 0 and first.uploaded == 1
        [claimed_id] = [n for n in fake.nodes if fake.nodes[n]["name"] == "shared.txt"]

        engine_b = UploadOnlySyncEngine(drive_client=fake, database=db, rules=SyncRules())
        second = engine_b.sync_up(root_b, "ElectriDrive/Shared")
    finally:
        db.close()

    # Pair A's file must survive untouched; pair B's unrelated file still syncs.
    assert fake.nodes[claimed_id]["content"] == b"from-pair-a"
    assert second.uploaded == 1  # unique-b.txt
    assert second.failed >= 1
    assert any("already tracked by a different sync pair" in e for e in second.errors)


def test_engine_renamed_local_file_without_collision_uploads_as_new(tmp_path: Path) -> None:
    fake = FakeDrive()
    root = tmp_path / "local"
    root.mkdir()
    (root / "old.txt").write_bytes(b"content")
    db = SyncDatabase(tmp_path / "db.sqlite3")
    try:
        engine = UploadOnlySyncEngine(drive_client=fake, database=db, rules=SyncRules())
        first = engine.sync_up(root, "ElectriDrive/Rename")
        assert first.failed == 0

        (root / "old.txt").rename(root / "new.txt")
        second = UploadOnlySyncEngine(drive_client=fake, database=db, rules=SyncRules()).sync_up(
            root, "ElectriDrive/Rename"
        )
    finally:
        db.close()

    assert second.failed == 0
    assert second.uploaded == 1
    names = sorted(n["name"] for n in fake.nodes.values() if n["content"] is not None)
    assert names == ["new.txt", "old.txt"]  # old remote object is orphaned, not deleted/overwritten


def test_engine_renamed_local_file_into_collision_is_blocked(tmp_path: Path) -> None:
    fake = FakeDrive()
    root_a = tmp_path / "pair-a"
    root_a.mkdir()
    (root_a / "old.txt").write_bytes(b"from-pair-a")
    root_b = tmp_path / "pair-b"
    root_b.mkdir()
    (root_b / "new.txt").write_bytes(b"from-pair-b")

    db = SyncDatabase(tmp_path / "db.sqlite3")
    try:
        UploadOnlySyncEngine(drive_client=fake, database=db, rules=SyncRules()).sync_up(
            root_b, "ElectriDrive/RenameCollision"
        )
        [claimed_id] = [n for n in fake.nodes if fake.nodes[n]["name"] == "new.txt"]

        engine_a = UploadOnlySyncEngine(drive_client=fake, database=db, rules=SyncRules())
        first = engine_a.sync_up(root_a, "ElectriDrive/RenameCollision")
        assert first.failed == 0

        (root_a / "old.txt").rename(root_a / "new.txt")
        second = UploadOnlySyncEngine(drive_client=fake, database=db, rules=SyncRules()).sync_up(
            root_a, "ElectriDrive/RenameCollision"
        )
    finally:
        db.close()

    assert fake.nodes[claimed_id]["content"] == b"from-pair-b"
    assert second.failed >= 1
    assert any("already tracked by a different sync pair" in e for e in second.errors)


def test_engine_stale_remote_id_still_alive_is_followed_not_overwritten_elsewhere(tmp_path: Path) -> None:
    fake = FakeDrive()
    root = tmp_path / "local"
    root.mkdir()
    (root / "stale.txt").write_bytes(b"v1")
    db = SyncDatabase(tmp_path / "db.sqlite3")
    try:
        first = UploadOnlySyncEngine(drive_client=fake, database=db, rules=SyncRules()).sync_up(
            root, "ElectriDrive/Stale"
        )
        assert first.failed == 0
        [tracked_id] = [n for n in fake.nodes if fake.nodes[n]["name"] == "stale.txt"]

        # Someone else renames our tracked file away, then a different,
        # unrelated file appears under our original name.
        fake.nodes[tracked_id]["name"] = "stale-moved-elsewhere.txt"
        unrelated_id = fake.add_file("stale.txt", b"unrelated", parent=fake.ensure_folder_path("ElectriDrive/Stale"))

        (root / "stale.txt").write_bytes(b"v2")
        second = UploadOnlySyncEngine(drive_client=fake, database=db, rules=SyncRules()).sync_up(
            root, "ElectriDrive/Stale"
        )
    finally:
        db.close()

    assert second.failed == 0
    assert fake.nodes[tracked_id]["content"] == b"v2"  # followed by ID, not by name
    assert fake.nodes[unrelated_id]["content"] == b"unrelated"  # left untouched


def test_engine_stale_remote_id_trashed_blocks_rather_than_overwriting_unrelated_file(tmp_path: Path) -> None:
    fake = FakeDrive()
    root = tmp_path / "local"
    root.mkdir()
    (root / "stale.txt").write_bytes(b"v1")
    db = SyncDatabase(tmp_path / "db.sqlite3")
    try:
        first = UploadOnlySyncEngine(drive_client=fake, database=db, rules=SyncRules()).sync_up(
            root, "ElectriDrive/StaleTrashed"
        )
        assert first.failed == 0
        [tracked_id] = [n for n in fake.nodes if fake.nodes[n]["name"] == "stale.txt"]

        fake.trash(tracked_id)
        unrelated_id = fake.add_file(
            "stale.txt", b"unrelated", parent=fake.ensure_folder_path("ElectriDrive/StaleTrashed")
        )

        (root / "stale.txt").write_bytes(b"v2")
        second = UploadOnlySyncEngine(drive_client=fake, database=db, rules=SyncRules()).sync_up(
            root, "ElectriDrive/StaleTrashed"
        )
    finally:
        db.close()

    assert second.failed >= 1
    assert any("stale Drive ID" in e for e in second.errors)
    assert fake.nodes[unrelated_id]["content"] == b"unrelated"


# ------------------------------------------------------- twoway.py (sync --direction up_only)

def test_twoway_up_only_first_sync_adopts_untracked_existing_file(tmp_path: Path) -> None:
    fake = FakeDrive()
    root = tmp_path / "local"
    root.mkdir()
    (root / "shared.txt").write_bytes(b"local-content")
    remote_root = fake.ensure_folder_path("ElectriDrive/Wild")
    wild_id = fake.add_file("shared.txt", b"pre-existing, never tracked", parent=remote_root)

    db = SyncDatabase(tmp_path / "db.sqlite3")
    try:
        pair = _up_only_pair(root, "ElectriDrive/Wild")
        report = TwoWaySyncEngine(fake, db, pair, rules=SyncRules()).run()
    finally:
        db.close()

    assert report.failed == 0
    assert fake.nodes[wild_id]["content"] == b"local-content"


def test_twoway_up_only_separate_pair_refuses_to_overwrite_claimed_file(tmp_path: Path) -> None:
    fake = FakeDrive()
    root_a = tmp_path / "pair-a"
    root_a.mkdir()
    (root_a / "shared.txt").write_bytes(b"from-pair-a")
    root_b = tmp_path / "pair-b"
    root_b.mkdir()
    (root_b / "shared.txt").write_bytes(b"from-pair-b")

    db = SyncDatabase(tmp_path / "db.sqlite3")
    try:
        pair_a = _up_only_pair(root_a, "ElectriDrive/Shared")
        first = TwoWaySyncEngine(fake, db, pair_a, rules=SyncRules()).run()
        assert first.failed == 0
        [claimed_id] = [n for n in fake.nodes if fake.nodes[n]["name"] == "shared.txt"]

        pair_b = _up_only_pair(root_b, "ElectriDrive/Shared")
        second = TwoWaySyncEngine(fake, db, pair_b, rules=SyncRules()).run()
    finally:
        db.close()

    assert fake.nodes[claimed_id]["content"] == b"from-pair-a"
    assert second.failed >= 1
    assert any("already tracked by a different sync pair" in e for e in second.errors)


def test_twoway_up_only_renamed_local_file_into_collision_is_blocked(tmp_path: Path) -> None:
    fake = FakeDrive()
    root_a = tmp_path / "pair-a"
    root_a.mkdir()
    (root_a / "old.txt").write_bytes(b"from-pair-a")
    root_b = tmp_path / "pair-b"
    root_b.mkdir()
    (root_b / "new.txt").write_bytes(b"from-pair-b")

    db = SyncDatabase(tmp_path / "db.sqlite3")
    try:
        pair_b = _up_only_pair(root_b, "ElectriDrive/RenameCollision")
        TwoWaySyncEngine(fake, db, pair_b, rules=SyncRules()).run()
        [claimed_id] = [n for n in fake.nodes if fake.nodes[n]["name"] == "new.txt"]

        pair_a = _up_only_pair(root_a, "ElectriDrive/RenameCollision")
        first = TwoWaySyncEngine(fake, db, pair_a, rules=SyncRules()).run()
        assert first.failed == 0

        (root_a / "old.txt").rename(root_a / "new.txt")
        second = TwoWaySyncEngine(fake, db, pair_a, rules=SyncRules()).run()
    finally:
        db.close()

    assert fake.nodes[claimed_id]["content"] == b"from-pair-b"
    assert second.failed >= 1
    assert any("already tracked by a different sync pair" in e for e in second.errors)


def test_twoway_up_only_stale_remote_id_trashed_blocks_rather_than_overwriting(tmp_path: Path) -> None:
    fake = FakeDrive()
    root = tmp_path / "local"
    root.mkdir()
    (root / "stale.txt").write_bytes(b"v1")
    db = SyncDatabase(tmp_path / "db.sqlite3")
    try:
        pair = _up_only_pair(root, "ElectriDrive/StaleTrashed")
        first = TwoWaySyncEngine(fake, db, pair, rules=SyncRules()).run()
        assert first.failed == 0
        [tracked_id] = [n for n in fake.nodes if fake.nodes[n]["name"] == "stale.txt"]

        fake.trash(tracked_id)
        unrelated_id = fake.add_file(
            "stale.txt", b"unrelated", parent=fake.ensure_folder_path("ElectriDrive/StaleTrashed")
        )

        (root / "stale.txt").write_bytes(b"v2")
        second = TwoWaySyncEngine(fake, db, pair, rules=SyncRules()).run()
    finally:
        db.close()

    assert second.failed >= 1
    assert fake.nodes[unrelated_id]["content"] == b"unrelated"
