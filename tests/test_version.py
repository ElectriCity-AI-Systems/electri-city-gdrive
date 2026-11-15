import re
from pathlib import Path

import electridrive
from electridrive.config import APP_VERSION


def test_release_version_is_consistent():
    root = Path(__file__).resolve().parents[1]
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)

    assert match is not None
    assert APP_VERSION == electridrive.__version__ == match.group(1) == "2.2.2"
    for workflow in ("customer-candidate.yml", "release-draft.yml"):
        text = (root / ".github" / "workflows" / workflow).read_text(encoding="utf-8")
        assert f"electridrive_{APP_VERSION}_amd64.deb" in text
    assert (root / "packaging" / f"release-{APP_VERSION}.md").is_file()
