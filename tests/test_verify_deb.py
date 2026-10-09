from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERIFY = ROOT / "scripts" / "verify_deb.py"


def _package(
    tmp_path: Path, exec_name: str, install_target: bool,
    depends: str = (
        "libfuse2t64 | libfuse2, fuse3 | fuse, libegl1, libgl1, "
        "libwayland-cursor0, libwayland-egl1"
    ),
) -> Path:
    root = tmp_path / "root"
    (root / "DEBIAN").mkdir(parents=True)
    (root / "usr/share/applications").mkdir(parents=True)
    (root / "usr/bin").mkdir(parents=True)
    (root / "opt/electridrive/_internal/PySide6/Qt/lib").mkdir(parents=True)
    (root / "opt/electridrive/_internal/PySide6/Qt/plugins/platforms").mkdir(parents=True)
    (root / "DEBIAN/control").write_text(
        "Package: electridrive\n"
        "Version: 9.9.9\n"
        "Architecture: amd64\n"
        f"Depends: {depends}\n"
        "Maintainer: Test <test@example.invalid>\n"
        "Description: package verifier fixture\n",
        encoding="utf-8",
    )
    (root / "usr/share/applications/electridrive.desktop").write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=ElectriDrive\n"
        f"Exec={exec_name}\n"
        f"TryExec={exec_name}\n",
        encoding="utf-8",
    )
    for filename in (
        "libQt6Core.so.6",
        "libQt6Gui.so.6",
        "libQt6Widgets.so.6",
    ):
        (root / "opt/electridrive/_internal/PySide6/Qt/lib" / filename).touch()
    for filename in ("libqoffscreen.so", "libqxcb.so"):
        (root / "opt/electridrive/_internal/PySide6/Qt/plugins/platforms" / filename).touch()

    if install_target:
        executable = root / "opt/electridrive/electridrive"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
        os.symlink("/opt/electridrive/electridrive", root / "usr/bin/electridrive")

    package = tmp_path / "fixture.deb"
    subprocess.run(
        ["dpkg-deb", "--build", "--root-owner-group", str(root), str(package)],
        check=True,
        capture_output=True,
        text=True,
    )
    return package


def _verify(package: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(VERIFY),
            str(package),
            "--expected-version",
            "9.9.9",
            "--max-glibc",
            "999.0",
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_rejects_desktop_command_missing_from_package(tmp_path: Path):
    result = _verify(_package(tmp_path, "electridrive-gui", install_target=False))

    assert result.returncode == 1
    assert "Exec command 'electridrive-gui' is absent from package" in result.stderr
    assert "TryExec command 'electridrive-gui' is absent from package" in result.stderr


def test_accepts_executable_desktop_command_and_absolute_symlink(tmp_path: Path):
    result = _verify(_package(tmp_path, "electridrive", install_target=True))

    assert result.returncode == 0, result.stderr
    assert "Exec=electridrive -> /opt/electridrive/electridrive (executable)" in result.stdout
    assert "TryExec=electridrive -> /opt/electridrive/electridrive (executable)" in result.stdout


def test_rejects_fuse3_library_without_fuse2_and_mount_helper(tmp_path: Path):
    package = _package(
        tmp_path, "electridrive", install_target=True,
        depends="libfuse3-3, libegl1, libgl1, libwayland-cursor0, libwayland-egl1",
    )
    result = _verify(package)

    assert result.returncode == 1
    assert "missing Debian runtime dependency: libfuse2t64 | libfuse2" in result.stderr
    assert "missing Debian runtime dependency: fuse3 | fuse" in result.stderr
