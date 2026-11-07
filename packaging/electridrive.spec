# PyInstaller spec for ElectriDrive (GUI).
# Build from the repo root: pyinstaller --noconfirm packaging/electridrive.spec
import os
import sys
from fnmatch import fnmatch
from pathlib import Path

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
sys.path.insert(0, ROOT)

from electridrive.build_gate import validate_build_inputs

_gate_errors = validate_build_inputs(
    Path(ROOT) / "electridrive",
    expected_revision=os.environ.get("GITHUB_SHA"),
    check_source_checkout=True,
)
if _gate_errors:
    raise SystemExit("PyInstaller release gate rejected this build: " + "; ".join(_gate_errors))

from PyInstaller.utils.hooks import collect_all

datas = [(os.path.join(ROOT, "assets", "electridrive.png"), "assets")]
binaries = []
hiddenimports = ["fuse", "google_auth_httplib2"]

# Collect packages that ship data files / use dynamic imports. PySide6 has a built-in hook.
# Narrow google.* to what we use (avoids pulling api_core/grpc).
for pkg in ("googleapiclient", "google.auth", "google.oauth2", "google_auth_oauthlib",
            "keyring", "watchdog"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

baked = os.path.join(ROOT, "electridrive", "google_api", "client_baked.json")
if os.path.exists(baked):
    datas += [(baked, "electridrive/google_api")]

channel = os.path.join(ROOT, "electridrive", "build_channel.txt")
datas += [(channel, "electridrive")]
provenance = os.path.join(ROOT, "electridrive", "build_provenance.json")
datas += [(provenance, "electridrive")]
qa_marker = os.path.join(ROOT, "electridrive", "QA-NOT-FOR-DISTRIBUTION.txt")
if os.path.exists(qa_marker):
    datas += [(qa_marker, "electridrive")]

a = Analysis(
    [os.path.join(ROOT, "app.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "PyQt5", "PyQt6", "grpc"],
    noarchive=False,
)
# GIO loads desktop modules from the host. Bundled GLib or its ABI-linked
# dependencies can shadow newer host libraries and break dconf/GVfs modules.
# Keep the complete native GLib dependency family on the host instead.
system_desktop_libraries = (
    "libglib-2.0.so*", "libgobject-2.0.so*", "libgio-2.0.so*",
    "libgmodule-2.0.so*", "libgthread-2.0.so*", "libpcre.so*",
    "libpcre2-8.so*", "libffi.so*", "libmount.so*", "libselinux.so*",
    "libblkid.so*", "libz.so*",
)
a.binaries = [
    entry for entry in a.binaries
    if not any(fnmatch(os.path.basename(entry[0]), pattern)
               for pattern in system_desktop_libraries)
]
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="electridrive",
    console=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="electridrive")
