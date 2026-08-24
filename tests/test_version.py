import re
from pathlib import Path

import electridrive
from electridrive.config import APP_VERSION


def test_release_version_is_consistent():
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)

    assert match is not None
    assert APP_VERSION == electridrive.__version__ == match.group(1) == "2.1.0"
