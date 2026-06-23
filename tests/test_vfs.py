from pathlib import Path

from electridrive.vfs import DriveTree, fuse_available
from fakes import FakeDrive


def _tree(tmp_path: Path):
    fake = FakeDrive()
    projects = fake.add_folder("Projects")
    fake.add_file("a.txt", b"hello-a", parent=projects)
    fake.add_file("Memo", b"memo", mime="application/vnd.google-apps.document")
    return fake, DriveTree(fake, "root", tmp_path / "cache")


def test_resolve_and_list(tmp_path: Path):
    fake, tree = _tree(tmp_path)
    assert tree.resolve("/") is None  # root is implicit
    names = {f.name for f in tree.list_dir("/")}
    assert {"Projects", "Memo"} <= names
    node = tree.resolve("/Projects/a.txt")
    assert node is not None and node.name == "a.txt"
    assert tree.resolve("/Projects/missing") is None


def test_ensure_cached_downloads_once(tmp_path: Path):
    fake, tree = _tree(tmp_path)
    node = tree.resolve("/Projects/a.txt")
    path = tree.ensure_cached(node)
    assert path.read_bytes() == b"hello-a"
    # cached now; path stable on second call
    assert tree.ensure_cached(node) == path


def test_ensure_cached_exports_google_doc(tmp_path: Path):
    fake, tree = _tree(tmp_path)
    node = tree.resolve("/Memo")
    path = tree.ensure_cached(node)
    assert path.read_bytes() == b"EXPORTED:memo"


def test_cache_invalidate_reflects_new_files(tmp_path: Path):
    fake, tree = _tree(tmp_path)
    assert "later.txt" not in {f.name for f in tree.list_dir("/")}
    fake.add_file("later.txt", b"x")
    tree.invalidate()
    assert "later.txt" in {f.name for f in tree.list_dir("/")}


def test_fuse_available_returns_bool():
    assert isinstance(fuse_available(), bool)
