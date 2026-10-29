from pathlib import Path

import pytest

from electridrive.sync.downloader import plan_download, sanitize_name
from fakes import FakeDrive


def test_sanitize_name_strips_separators():
    assert sanitize_name("a/b") == "a_b"
    assert sanitize_name("  spaced.  ") == "spaced"
    assert sanitize_name("") == "untitled"


def test_plan_download_walks_folder_tree(tmp_path: Path):
    fake = FakeDrive()
    projects = fake.add_folder("Projects")
    fake.add_file("a.txt", b"hello", parent=projects)
    sub = fake.add_folder("sub", parent=projects)
    fake.add_file("b.txt", b"world", parent=sub)

    remote = fake.get_metadata(projects)
    items = plan_download(fake, remote, tmp_path)

    dests = {str(i.dest_path.relative_to(tmp_path)) for i in items}
    assert dests == {"Projects/a.txt", "Projects/sub/b.txt"}
    assert all(not i.is_google_doc for i in items)


def test_plan_download_google_doc_marks_export(tmp_path: Path):
    fake = FakeDrive()
    doc = fake.add_file("Notes", b"x", mime="application/vnd.google-apps.document")
    remote = fake.get_metadata(doc)

    items = plan_download(fake, remote, tmp_path)
    assert len(items) == 1
    item = items[0]
    assert item.is_google_doc
    assert item.dest_path.name == "Notes.docx"
    assert item.export_mime == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )


def test_manual_download_rejects_pagination_cycle(tmp_path: Path):
    from electridrive.google_api.client import FileListing

    class CyclingDrive(FakeDrive):
        calls = 0

        def list_folder(self, parent_id="root", page_token=None):
            self.calls += 1
            if self.calls > 3:
                raise RuntimeError("test watchdog: infinite pagination")
            return FileListing([], "again")

    fake = CyclingDrive()
    folder = fake.add_folder("QA")
    with pytest.raises(RuntimeError, match="repeated page token"):
        plan_download(fake, fake.get_metadata(folder), tmp_path)
