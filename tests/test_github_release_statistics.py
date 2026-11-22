"""Offline regression tests for GitHub release download statistics."""
import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from io import BytesIO

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "github_release_statistics.py"
spec = importlib.util.spec_from_file_location("release_statistics", SCRIPT)
stats = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stats)

class ReleaseStatisticsTests(unittest.TestCase):
    def test_only_installers_and_non_draft(self):
        releases = [
            {"tag_name": "v2.2.2", "prerelease": True, "draft": False, "assets": [
                {"id": 1, "name": "ElectriDrive-x86_64.AppImage", "download_count": 7},
                {"id": 2, "name": "electridrive_2.2.2_amd64.deb", "download_count": 3},
                {"id": 3, "name": "SHA256SUMS.txt", "download_count": 99},
                {"id": 4, "name": "candidate-manifest.json", "download_count": 99}]},
            {"tag_name": "draft", "draft": True, "assets": [
                {"id": 5, "name": "secret.deb", "download_count": 12}]}]
        rows = stats.snapshot(releases, "2026-11-22T00:00:00Z")
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(row["downloads_total"] for row in rows), 10)
        self.assertTrue(all(row["prerelease"] for row in rows))
        self.assertTrue(all(row["captured_utc"] == "2026-11-22T00:00:00Z" for row in rows))

    def test_empty_releases(self):
        self.assertEqual(stats.snapshot([]), [])

    def test_pagination(self):
        class FakeResponse:
            def __init__(self, value):
                self.value = value
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
        pages = [FakeResponse(BytesIO(json.dumps([{"tag_name": "x"}] * 100).encode())),
                 FakeResponse(BytesIO(b"[]"))]
        with patch.object(stats.urllib.request, "urlopen", side_effect=pages) as get:
            self.assertEqual(len(stats.fetch_releases()), 100)
            self.assertEqual(get.call_count, 2)

if __name__ == "__main__":
    unittest.main()
