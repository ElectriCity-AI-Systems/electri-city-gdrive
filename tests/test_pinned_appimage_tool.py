"""Offline fail-closed tests of immutable AppImage build dependencies."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("kind", ["corrupt-cache", "symlink"])
def test_pinned_appimagetool_fails_closed_without_download(tmp_path: Path, kind: str):
    script = Path(__file__).resolve().parents[1] / "scripts/get_pinned_appimagetool.sh"
    assert script.is_file()
    tool = tmp_path / "build/appimagetool-x86_64.AppImage"
    tool.parent.mkdir(parents=True)
    if kind == "corrupt-cache":
        tool.write_bytes(b"not the pinned trusted binary")
        before = tool.read_bytes()
    else:
        target = tmp_path / "important-user-file"
        target.write_bytes(b"do not overwrite")
        tool.symlink_to(target)
        before = target.read_bytes()

    result = subprocess.run(
        ["bash", str(script)],
        cwd=tmp_path, capture_output=True, text=True,
        timeout=8, check=False,
    )
    assert result.returncode != 0
    assert "ERROR:" in result.stderr
    if kind == "corrupt-cache":
        assert tool.read_bytes() == before
    else:
        assert tool.is_symlink() and target.read_bytes() == before


def test_pinned_appimage_build_script_has_versioned_dependencies():
    script = Path(__file__).resolve().parents[1] / "scripts/get_pinned_appimagetool.sh"
    body = script.read_text(encoding="utf-8")
    assert 'TOOL_VERSION="1.9.1"' in body
    assert 'RUNTIME_VERSION="20251108"' in body
    assert "releases/download/continuous/" not in body
    assert "sha256sum --check --status" in body
    assert "TOOL_SHA256" in body and "RUNTIME_SHA256" in body
    for name in ("scripts/build_release.sh", "scripts/build_appimage.sh"):
        caller = (Path(__file__).resolve().parents[1] / name).read_text()
        assert "bash scripts/get_pinned_appimagetool.sh" in caller
        assert '--runtime-file "$RUNTIME"' in caller
