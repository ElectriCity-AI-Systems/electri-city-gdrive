from pathlib import Path

from electridrive.google_api.client import DRIVE_FOLDER_MIME, RemoteFile
from electridrive.storage.database import SyncDatabase
from electridrive.sync.uploader import plan_upload
from fakes import FakeDrive


def test_plan_upload_single_file(tmp_path: Path):
    f = tmp_path / "a.txt"
    f.write_text("hi", encoding="utf-8")
    fake = FakeDrive()
    items = plan_upload(fake, f, "root")
    assert len(items) == 1
    assert items[0].name == "a.txt"
    assert items[0].parent_id == "root"


def test_plan_upload_folder_mirrors_tree_and_excludes(tmp_path: Path):
    root = tmp_path / "proj"
    (root / "sub").mkdir(parents=True)
    (root / "keep.txt").write_text("a", encoding="utf-8")
    (root / "sub" / "deep.txt").write_text("b", encoding="utf-8")
    # excluded by default rules:
    (root / ".secret").write_text("no", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "x.js").write_text("no", encoding="utf-8")

    fake = FakeDrive()
    items = plan_upload(fake, root, "root")

    names = sorted(i.name for i in items)
    assert names == ["deep.txt", "keep.txt"]

    # remote folders "proj" and "proj/sub" were created in the fake
    proj = fake.find_folder("proj", "root")
    assert proj is not None
    assert fake.find_folder("sub", proj) is not None


def test_plan_upload_reuses_existing_remote_folder(tmp_path: Path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.txt").write_text("a", encoding="utf-8")
    fake = FakeDrive()
    existing = fake.add_folder("proj", "root")

    plan_upload(fake, root, "root")
    # no duplicate "proj" folder should be created
    projs = [n for n in fake.nodes.values() if n["name"] == "proj"]
    assert len(projs) == 1
    assert projs[0]["id"] == existing


def test_plan_upload_resolves_folders_from_cache_without_find_folder(tmp_path: Path):
    root = tmp_path / "proj"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "a.txt").write_text("a", encoding="utf-8")

    fake = FakeDrive()
    proj = fake.add_folder("proj", "root")
    sub = fake.add_folder("sub", proj)
    db = SyncDatabase(tmp_path / "state.sqlite3")
    try:
        # Seed the remote-node cache so existing folders resolve with no network call.
        db.cache_remote_children("root", [RemoteFile(id=proj, name="proj",
                                          mime_type=DRIVE_FOLDER_MIME, parents=("root",))])
        db.cache_remote_children(proj, [RemoteFile(id=sub, name="sub",
                                        mime_type=DRIVE_FOLDER_MIME, parents=(proj,))])

        before = fake.find_folder_calls
        items = plan_upload(fake, root, "root", db=db)

        assert fake.find_folder_calls == before  # every folder came from the cache
        assert sorted(i.name for i in items) == ["a.txt"]
        assert items[0].parent_id == sub
        assert len([n for n in fake.nodes.values() if n["name"] == "proj"]) == 1
        assert len([n for n in fake.nodes.values() if n["name"] == "sub"]) == 1
    finally:
        db.close()
