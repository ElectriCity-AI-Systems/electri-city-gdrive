from __future__ import annotations

import os
from pathlib import Path

from electridrive.config import Settings, SyncPair, load_settings, save_settings
from electridrive.google_api.client import export_format_for
from electridrive.storage.database import SyncDatabase
from electridrive.sync.downloader import plan_download, sanitize_name
from electridrive.sync.engine import UploadOnlySyncEngine
from electridrive.sync.runner import run_configured_pairs
from electridrive.sync.twoway import TwoWaySyncEngine
from electridrive.sync.uploader import plan_upload
from fakes import FakeDrive


FILES = {
    "root.txt": b"root",
    "Folder A/a.txt": b"alpha",
    "Folder A/Level 2/b.txt": b"beta",
    "Folder A/Level 2/Level 3/Level 4/deep.txt": b"deep",
    "Unicode äöü/file with spaces.txt": b"unicode",
}
FOLDERS = {
    "Folder A",
    "Folder A/Empty Folder",
    "Folder A/Empty Folder/Nested Empty",
    "Folder A/Empty Folder/Nested Empty/Leaf Empty",
    "Folder A/Level 2",
    "Folder A/Level 2/Level 3",
    "Folder A/Level 2/Level 3/Level 4",
    "Unicode äöü",
}
MAX_FOLDER_DEPTH = 4


def _build_local_tree(root: Path) -> None:
    for rel in sorted(FOLDERS, key=lambda item: (item.count("/"), item)):
        (root / rel).mkdir(parents=True, exist_ok=True)
    for rel, content in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def _seed_remote_tree(fake: FakeDrive, root_id: str) -> None:
    ids = {"": root_id}
    for rel in sorted(FOLDERS, key=lambda item: (item.count("/"), item)):
        path = Path(rel)
        parent = path.parent.as_posix()
        parent = "" if parent == "." else parent
        ids[rel] = fake.add_folder(path.name, ids[parent])
    for rel, content in FILES.items():
        path = Path(rel)
        parent = path.parent.as_posix()
        parent = "" if parent == "." else parent
        fake.add_file(path.name, content, parent=ids[parent])


def _remote_manifest(
    fake: FakeDrive, root_id: str, *, effective_workspace_names: bool = False
) -> tuple[set[str], set[str]]:
    files: set[str] = set()
    folders: set[str] = set()

    def walk(parent_id: str, prefix: str) -> None:
        children = [
            node
            for node in fake.nodes.values()
            if node["parent"] == parent_id and not node["trashed"]
        ]
        for node in sorted(children, key=lambda item: item["name"]):
            name = sanitize_name(node["name"])
            is_folder = node["mime"] == "application/vnd.google-apps.folder"
            if effective_workspace_names and not is_folder and node["mime"].startswith(
                "application/vnd.google-apps."
            ):
                _mime, extension = export_format_for(node["mime"])
                if not name.lower().endswith(extension):
                    name += extension
            rel = f"{prefix}/{name}" if prefix else name
            if is_folder:
                folders.add(rel)
                walk(node["id"], rel)
            else:
                files.add(rel)

    walk(root_id, "")
    return files, folders


def _local_manifest(root: Path) -> tuple[set[str], set[str]]:
    files: set[str] = set()
    folders: set[str] = set()
    for current, dir_names, file_names in os.walk(root, followlinks=False):
        dir_names[:] = sorted(
            name for name in dir_names if name != ".electridrive-trash"
        )
        relative_dir = Path(current).relative_to(root)
        for name in dir_names:
            rel = (relative_dir / name).as_posix()
            folders.add(rel)
        for name in sorted(file_names):
            files.add((relative_dir / name).as_posix())
    return files, folders


def _assert_complete(
    expected_files: set[str],
    expected_folders: set[str],
    actual: tuple[set[str], set[str]],
) -> None:
    actual_files, actual_folders = actual
    assert actual_files == expected_files, {
        "missing": sorted(expected_files - actual_files),
        "unexpected": sorted(actual_files - expected_files),
    }
    assert actual_folders == expected_folders, {
        "missing": sorted(expected_folders - actual_folders),
        "unexpected": sorted(actual_folders - expected_folders),
    }


def _remote_id(fake: FakeDrive, root_id: str, rel: str) -> str:
    parent = root_id
    for part in Path(rel).parts:
        matches = [
            node
            for node in fake.nodes.values()
            if node["parent"] == parent
            and node["name"] == part
            and not node["trashed"]
        ]
        assert len(matches) == 1, (rel, matches)
        parent = matches[0]["id"]
    return parent


def _assert_no_duplicate_children(fake: FakeDrive) -> None:
    seen: set[tuple[str | None, str]] = set()
    for node in fake.nodes.values():
        if node["trashed"] or node["id"] == "root":
            continue
        identity = (node["parent"], node["name"])
        assert identity not in seen, identity
        seen.add(identity)


def test_upload_only_syncs_exact_recursive_tree_empty_folders_and_reuses_ids(
    tmp_path: Path,
) -> None:
    root = tmp_path / "selected-root"
    _build_local_tree(root)
    (root / ".secret").write_bytes(b"excluded")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "ignored.js").write_bytes(b"excluded")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "outside.txt").write_bytes(b"must not sync")
    (root / "outside-link").symlink_to(outside, target_is_directory=True)
    (root / "outside-file-link").symlink_to(outside / "outside.txt")

    fake = FakeDrive()
    remote_root = fake.ensure_folder_path("ElectriDrive/Complete Upload")
    existing_folder = fake.add_folder("Folder A", remote_root)
    existing_file = fake.add_file("root.txt", b"old", parent=remote_root)
    db = SyncDatabase(tmp_path / "upload.sqlite3")
    try:
        engine = UploadOnlySyncEngine(drive_client=fake, database=db)
        first = engine.sync_up(root, "ElectriDrive/Complete Upload")

        assert first.scanned == len(FILES)
        assert first.scanned_folders == len(FOLDERS)
        assert first.folders_synced == len(FOLDERS)
        assert first.excluded == 4
        assert first.uploaded == len(FILES)
        assert first.failed == first.unexplained_omissions == 0
        _assert_complete(set(FILES), FOLDERS, _remote_manifest(fake, remote_root))
        assert _remote_id(fake, remote_root, "Folder A") == existing_folder
        assert _remote_id(fake, remote_root, "root.txt") == existing_file
        _assert_no_duplicate_children(fake)

        upload_count = len(fake.uploads)
        update_count = len(fake.updates)
        second = engine.sync_up(root, "ElectriDrive/Complete Upload")
        assert second.uploaded == 0
        assert second.skipped == len(FILES)
        assert len(fake.uploads) == upload_count
        assert len(fake.updates) == update_count

        deep = root / "Folder A/Level 2/Level 3/Level 4/deep.txt"
        original_id = _remote_id(
            fake, remote_root, "Folder A/Level 2/Level 3/Level 4/deep.txt"
        )
        deep.write_bytes(b"deep changed")
        changed = engine.sync_up(root, "ElectriDrive/Complete Upload")
        assert changed.uploaded == 1 and changed.failed == 0
        assert _remote_id(
            fake, remote_root, "Folder A/Level 2/Level 3/Level 4/deep.txt"
        ) == original_id
        assert fake.nodes[original_id]["content"] == b"deep changed"
        _assert_no_duplicate_children(fake)
    finally:
        db.close()


def test_download_only_syncs_exact_recursive_tree_and_remote_empty_folders(
    tmp_path: Path,
) -> None:
    fake = FakeDrive()
    remote_root = fake.ensure_folder_path("ElectriDrive/Complete Download")
    _seed_remote_tree(fake, remote_root)
    fake.add_file(".hidden", b"excluded", parent=remote_root)
    hidden_folder = fake.add_folder(".private", remote_root)
    fake.add_file("inside.txt", b"excluded", parent=hidden_folder)
    local_root = tmp_path / "not-created-yet"
    pair = SyncPair(
        str(local_root), "ElectriDrive/Complete Download", direction="down_only"
    )
    db = SyncDatabase(tmp_path / "download.sqlite3")
    try:
        engine = TwoWaySyncEngine(fake, db, pair)
        first = engine.run()
        assert first.downloaded == len(FILES)
        assert first.folders_created_local == len(FOLDERS)
        assert first.excluded_remote == 2
        assert first.failed == first.unexplained_omissions == 0
        _assert_complete(set(FILES), FOLDERS, _local_manifest(local_root))
        assert {item.local_rel for item in db.list_sync_folders(engine.pair_id)} == FOLDERS

        second = engine.run()
        assert (second.uploaded, second.downloaded, second.failed) == (0, 0, 0)
        assert second.excluded_remote == 2

        rel = "Folder A/Level 2/Level 3/Level 4/deep.txt"
        deep_id = _remote_id(fake, remote_root, rel)
        fake.modify_file(deep_id, b"remote changed", modified="2026-08-24T10:00:00Z")
        changed = engine.run()
        assert changed.downloaded == 1 and changed.failed == 0
        assert (local_root / rel).read_bytes() == b"remote changed"
    finally:
        db.close()


def test_two_way_complete_union_updates_conflicts_exports_and_safe_deletes(
    tmp_path: Path,
) -> None:
    local_root = tmp_path / "selected-root"
    _build_local_tree(local_root)
    fake = FakeDrive()
    remote_root = fake.ensure_folder_path("ElectriDrive/Complete Two Way")
    fake.add_file("root.txt", FILES["root.txt"], parent=remote_root)
    remote_only = fake.add_folder("Remote Only", remote_root)
    fake.add_folder("Empty Remote", remote_only)
    remote_file_id = fake.add_file("remote.txt", b"remote", parent=remote_only)

    expected_files = set(FILES) | {"Remote Only/remote.txt"}
    expected_folders = FOLDERS | {"Remote Only", "Remote Only/Empty Remote"}
    pair = SyncPair(str(local_root), "ElectriDrive/Complete Two Way")
    db = SyncDatabase(tmp_path / "two-way.sqlite3")
    try:
        engine = TwoWaySyncEngine(fake, db, pair)
        first = engine.run()
        assert first.recorded == 1
        assert first.uploaded == len(FILES) - 1
        assert first.downloaded == 1
        assert first.failed == first.unexplained_omissions == 0
        _assert_complete(expected_files, expected_folders, _local_manifest(local_root))
        _assert_complete(expected_files, expected_folders, _remote_manifest(fake, remote_root))
        _assert_no_duplicate_children(fake)

        unchanged = engine.run()
        assert (
            unchanged.uploaded,
            unchanged.downloaded,
            unchanged.trashed_local,
            unchanged.trashed_remote,
            unchanged.failed,
        ) == (0, 0, 0, 0, 0)

        deep_rel = "Folder A/Level 2/Level 3/Level 4/deep.txt"
        deep_id = _remote_id(fake, remote_root, deep_rel)
        (local_root / deep_rel).write_bytes(b"local changed")
        local_change = engine.run()
        assert local_change.uploaded == 1 and local_change.failed == 0
        assert _remote_id(fake, remote_root, deep_rel) == deep_id
        assert fake.nodes[deep_id]["content"] == b"local changed"

        fake.modify_file(
            remote_file_id, b"remote changed", modified="2026-08-24T10:00:00Z"
        )
        remote_change = engine.run()
        assert remote_change.downloaded == 1 and remote_change.failed == 0
        assert (local_root / "Remote Only/remote.txt").read_bytes() == b"remote changed"

        (local_root / "new local root.txt").write_bytes(b"new local")
        fake.add_file("new remote root.txt", b"new remote", parent=remote_root)
        additions = engine.run()
        assert additions.uploaded == additions.downloaded == 1
        assert additions.failed == 0
        expected_files.update({"new local root.txt", "new remote root.txt"})

        deepest_folder = _remote_id(
            fake, remote_root, "Folder A/Level 2/Level 3/Level 4"
        )
        workspace = {
            "Deep Doc": "application/vnd.google-apps.document",
            "Deep Sheet": "application/vnd.google-apps.spreadsheet",
            "Deep Slides": "application/vnd.google-apps.presentation",
        }
        for name, mime in workspace.items():
            fake.add_file(name, name.encode(), parent=deepest_folder, mime=mime)
        exports = engine.run()
        assert exports.downloaded == 3 and exports.failed == 0
        for name, mime in workspace.items():
            _export_mime, extension = export_format_for(mime)
            rel = f"Folder A/Level 2/Level 3/Level 4/{name}{extension}"
            expected_files.add(rel)
            assert (local_root / rel).read_bytes() == b"EXPORTED:" + name.encode()

        # Both sides change a level-four file. The canonical ID is retained and
        # the losing bytes survive as a synchronized conflict copy in this run.
        fake.modify_file(deep_id, b"remote conflict", modified="2025-01-01T00:00:00Z")
        (local_root / deep_rel).write_bytes(b"local conflict winner")
        conflict = engine.run()
        assert conflict.conflicts == 1 and conflict.failed == 0
        conflict_files = list((local_root / Path(deep_rel).parent).glob("deep (conflict *).txt"))
        assert len(conflict_files) == 1
        assert conflict_files[0].read_bytes() == b"remote conflict"
        assert conflict.uploaded == 1
        conflict_rel = conflict_files[0].relative_to(local_root).as_posix()
        expected_files.add(conflict_rel)
        converged = engine.run()
        assert converged.uploaded == 0 and converged.failed == 0

        # Local and remote deletions use recoverable Trash behavior.
        local_deleted_rel = "Folder A/a.txt"
        (local_root / local_deleted_rel).unlink()
        local_delete = engine.run()
        assert local_delete.trashed_remote == 1 and local_delete.failed == 0
        expected_files.remove(local_deleted_rel)

        fake.trash(remote_file_id)
        remote_delete = engine.run()
        assert remote_delete.trashed_local == 1 and remote_delete.failed == 0
        expected_files.remove("Remote Only/remote.txt")
        assert (local_root / ".electridrive-trash/Remote Only/remote.txt").exists()

        # Empty-directory deletions are resolved conservatively by restoring the
        # directory, so no recursive content loss is inferred from folder state.
        local_leaf = local_root / "Folder A/Empty Folder/Nested Empty/Leaf Empty"
        local_leaf.rmdir()
        restored_local_folder = engine.run()
        assert restored_local_folder.folders_created_local == 1
        assert local_leaf.is_dir()

        remote_leaf_id = _remote_id(
            fake,
            remote_root,
            "Folder A/Empty Folder/Nested Empty/Leaf Empty",
        )
        fake.trash(remote_leaf_id)
        restored_remote_folder = engine.run()
        assert restored_remote_folder.folders_created_remote == 1
        assert _remote_id(
            fake,
            remote_root,
            "Folder A/Empty Folder/Nested Empty/Leaf Empty",
        ) != remote_leaf_id

        _assert_complete(expected_files, expected_folders, _local_manifest(local_root))
        _assert_complete(
            expected_files,
            expected_folders,
            _remote_manifest(fake, remote_root, effective_workspace_names=True),
        )
        _assert_no_duplicate_children(fake)
    finally:
        db.close()


def test_one_way_modes_keep_the_configured_side_authoritative(tmp_path: Path) -> None:
    fake = FakeDrive()
    upload_root = tmp_path / "upload-authority"
    upload_root.mkdir()
    (upload_root / "same.txt").write_bytes(b"local")
    upload_remote = fake.ensure_folder_path("ElectriDrive/Upload Authority")
    upload_id = fake.add_file("same.txt", b"remote", parent=upload_remote)
    db = SyncDatabase(tmp_path / "one-way.sqlite3")
    try:
        up = TwoWaySyncEngine(
            fake,
            db,
            SyncPair(
                str(upload_root), "ElectriDrive/Upload Authority", direction="up_only"
            ),
        )
        assert up.run().failed == 0
        assert fake.nodes[upload_id]["content"] == b"local"
        fake.modify_file(upload_id, b"remote edit", modified="2026-08-24T11:00:00Z")
        assert up.run().uploaded == 1
        assert (upload_root / "same.txt").read_bytes() == b"local"
        (upload_root / "same.txt").unlink()
        assert up.run().trashed_remote == 0
        assert fake.nodes[upload_id]["trashed"] is False

        native_id = fake.add_file(
            "Notes",
            b"native",
            parent=upload_remote,
            mime="application/vnd.google-apps.document",
        )
        (upload_root / "Notes.docx").write_bytes(b"local document")
        workspace_collision = up.run()
        assert workspace_collision.uploaded == 1
        assert workspace_collision.failed == 0
        assert fake.nodes[native_id]["content"] == b"native"
        binary_notes = [
            node
            for node in fake.nodes.values()
            if node["parent"] == upload_remote
            and node["name"] == "Notes.docx"
            and not node["trashed"]
        ]
        assert len(binary_notes) == 1
        assert binary_notes[0]["content"] == b"local document"

        download_root = tmp_path / "download-authority"
        download_root.mkdir()
        (download_root / "same.txt").write_bytes(b"local")
        download_remote = fake.ensure_folder_path("ElectriDrive/Download Authority")
        download_id = fake.add_file("same.txt", b"remote", parent=download_remote)
        down = TwoWaySyncEngine(
            fake,
            db,
            SyncPair(
                str(download_root),
                "ElectriDrive/Download Authority",
                direction="down_only",
            ),
        )
        assert down.run().failed == 0
        assert (download_root / "same.txt").read_bytes() == b"remote"
        (download_root / "same.txt").write_bytes(b"local edit")
        assert down.run().downloaded == 1
        assert (download_root / "same.txt").read_bytes() == b"remote"
        (download_root / "same.txt").unlink()
        assert down.run().downloaded == 1
        assert (download_root / "same.txt").read_bytes() == b"remote"
        assert fake.nodes[download_id]["trashed"] is False
    finally:
        db.close()


def test_saved_multiple_roots_all_run_and_one_failure_does_not_stop_later_pair(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    first_root = tmp_path / "first-root"
    second_root = tmp_path / "second-root"
    _build_local_tree(first_root)
    second_root.mkdir()
    (second_root / "second.txt").write_bytes(b"second")
    missing_root = tmp_path / "missing-root"
    disabled_root = tmp_path / "disabled-root"
    disabled_root.mkdir()
    (disabled_root / "disabled.txt").write_bytes(b"disabled")
    pairs = [
        SyncPair(str(first_root), "ElectriDrive/First", direction="up_only"),
        SyncPair(str(missing_root), "ElectriDrive/Missing", direction="up_only"),
        SyncPair(str(second_root), "ElectriDrive/Second", direction="two_way"),
        SyncPair(str(disabled_root), "ElectriDrive/Disabled", enabled=False),
    ]
    save_settings(Settings(sync_pairs=pairs))
    saved_pairs = load_settings().sync_pairs
    assert [(pair.local_path, pair.remote_folder) for pair in saved_pairs] == [
        (pair.local_path, pair.remote_folder) for pair in pairs
    ]

    fake = FakeDrive()
    db = SyncDatabase(tmp_path / "runner.sqlite3")
    try:
        result = run_configured_pairs(fake, db, saved_pairs)
        assert result.configured_pairs == 4
        assert result.enabled_pairs == 3
        assert result.successful_pairs == 2
        assert result.failed_pairs == 1
        assert len(result.outcomes) == 3
        assert result.outcomes[1].report.failed == 1

        first_remote = fake.ensure_folder_path("ElectriDrive/First")
        second_remote = fake.ensure_folder_path("ElectriDrive/Second")
        _assert_complete(set(FILES), FOLDERS, _remote_manifest(fake, first_remote))
        _assert_complete({"second.txt"}, set(), _remote_manifest(fake, second_remote))
        assert fake.find_folder("Disabled", fake.find_folder("ElectriDrive", "root")) is None
        _assert_no_duplicate_children(fake)
    finally:
        db.close()


def test_manual_planners_preserve_nested_empty_folders(tmp_path: Path) -> None:
    local = tmp_path / "Planner Root"
    (local / "Empty/Nested/Leaf").mkdir(parents=True)
    fake = FakeDrive()
    assert plan_upload(fake, local, "root") == []
    remote_root = fake.find_folder("Planner Root", "root")
    assert remote_root is not None
    _assert_complete(
        set(),
        {"Empty", "Empty/Nested", "Empty/Nested/Leaf"},
        _remote_manifest(fake, remote_root),
    )

    destination = tmp_path / "download"
    plan_download(fake, fake.get_metadata(remote_root), destination)
    _assert_complete(
        set(),
        {
            "Planner Root",
            "Planner Root/Empty",
            "Planner Root/Empty/Nested",
            "Planner Root/Empty/Nested/Leaf",
        },
        _local_manifest(destination),
    )


def test_remote_duplicate_is_an_explicit_failure_not_a_silent_omission(
    tmp_path: Path,
) -> None:
    local = tmp_path / "duplicate-destination"
    local.mkdir()
    fake = FakeDrive()
    remote = fake.ensure_folder_path("ElectriDrive/Duplicate Source")
    fake.add_file("duplicate.txt", b"one", parent=remote)
    fake.add_file("duplicate.txt", b"two", parent=remote)
    db = SyncDatabase(tmp_path / "duplicate.sqlite3")
    try:
        report = TwoWaySyncEngine(
            fake,
            db,
            SyncPair(
                str(local), "ElectriDrive/Duplicate Source", direction="down_only"
            ),
        ).run()
        assert report.failed == 1
        assert report.downloaded == 0
        assert any("duplicate remote file path" in error for error in report.errors)
        assert not (local / "duplicate.txt").exists()
    finally:
        db.close()


def test_failed_transfer_triggers_the_runtime_completeness_assertion(
    tmp_path: Path,
) -> None:
    class FailingDownloadDrive(FakeDrive):
        def download_file(self, file_id, dest_path, progress_cb=None):
            raise OSError("synthetic transfer failure")

    local = tmp_path / "failed-download"
    local.mkdir()
    fake = FailingDownloadDrive()
    remote = fake.ensure_folder_path("ElectriDrive/Failed Download")
    fake.add_file("required.txt", b"required", parent=remote)
    db = SyncDatabase(tmp_path / "failed-download.sqlite3")
    try:
        report = TwoWaySyncEngine(
            fake,
            db,
            SyncPair(
                str(local), "ElectriDrive/Failed Download", direction="down_only"
            ),
        ).run()
        assert report.failed >= 2
        assert report.unexplained_omissions == 1
        assert any("file:required.txt" in error for error in report.errors)
    finally:
        db.close()


def test_completeness_fixture_has_required_release_depth_and_counts() -> None:
    assert len(FILES) == 5
    assert len(FOLDERS) == 8
    assert max(path.count("/") + 1 for path in FOLDERS) == MAX_FOLDER_DEPTH
    assert "Folder A/Empty Folder" in FOLDERS
    assert "Folder A/Empty Folder/Nested Empty/Leaf Empty" in FOLDERS
