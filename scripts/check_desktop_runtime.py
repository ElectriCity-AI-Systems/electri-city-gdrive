#!/usr/bin/env python3
"""Check host GIO modules with the packaged application's library search path."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


PROBE = r'''
import ctypes
import json
from pathlib import Path

names = ["libglib-2.0.so", "libgobject-2.0.so", "libgmodule-2.0.so",
         "libgthread-2.0.so", "libgio-2.0.so"]
libraries = [ctypes.CDLL(name + ".0", mode=ctypes.RTLD_GLOBAL) for name in names]
gio = libraries[-1]
for name in ("g_vfs_get_default", "g_settings_backend_get_default"):
    function = getattr(gio, name)
    function.argtypes = []
    function.restype = ctypes.c_void_p
    function()
paths = sorted({
    line.split()[-1] for line in Path("/proc/self/maps").read_text().splitlines()
    if any(name in line for name in names)
})
print(json.dumps(paths))
'''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    bundle = parser.parse_args().bundle.resolve()
    if not bundle.is_dir():
        parser.error(f"bundle directory does not exist: {bundle}")
    env = os.environ.copy()
    original = env.get("LD_LIBRARY_PATH_ORIG", env.get("LD_LIBRARY_PATH", ""))
    env["LD_LIBRARY_PATH"] = os.pathsep.join(filter(None, (str(bundle), original)))
    result = subprocess.run(
        [sys.executable, "-c", PROBE], env=env, capture_output=True, text=True,
    )
    if result.returncode or "undefined symbol" in result.stderr or "Failed to load module" in result.stderr:
        print(result.stderr, file=sys.stderr)
        return 1
    try:
        paths = [Path(value).resolve() for value in json.loads(result.stdout)]
    except (ValueError, TypeError):
        print("GIO probe did not return library paths", file=sys.stderr)
        return 1
    if len(paths) != 5 or any(path.is_relative_to(bundle) for path in paths):
        print("GLib/GIO must load entirely from the host runtime", file=sys.stderr)
        return 1
    print("OK: host GLib/GIO desktop backends loaded without module ABI errors")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
