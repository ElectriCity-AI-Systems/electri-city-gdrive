# PyInstaller spec for ElectriDrive (GUI).
# Build from the repo root: pyinstaller --noconfirm packaging/electridrive.spec
import os
from fnmatch import fnmatch
from PyInstaller.utils.hooks import collect_all

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

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
