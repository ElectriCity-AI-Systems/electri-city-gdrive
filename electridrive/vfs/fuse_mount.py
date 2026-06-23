"""FUSE "Virtual Drive" — mount Google Drive as files-on-demand, without rclone.

The tree/cache logic lives in :class:`DriveTree` (pure, testable with FakeDrive).
The FUSE binding (`fusepy`) is imported lazily inside :func:`create_operations_class`
so importing this module never requires libfuse.

Prototype scope: read + directory listing + on-demand download into a local cache.
Google Docs are exported on first read. Writing through the mount is opt-in and
experimental.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import threading
from datetime import datetime
from itertools import count
from pathlib import Path
from time import time

LOGGER = logging.getLogger(__name__)


def fuse_available() -> bool:
    """True if both the python binding and a fusermount helper are present."""
    try:
        import fuse  # noqa: F401
    except Exception:
        return False
    return bool(shutil.which("fusermount3") or shutil.which("fusermount"))


def _parse_iso(iso: str | None) -> float:
    if not iso:
        return time()
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except Exception:
        return time()


class DriveTree:
    """Lazy, cached view of a Drive subtree for the virtual filesystem."""

    def __init__(self, client, root_id: str, cache_dir: Path, ttl: float = 30.0):
        self.client = client
        self.root_id = root_id
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl
        self._children: dict[str, tuple[float, dict]] = {}

    def invalidate(self):
        self._children.clear()

    def children(self, folder_id: str) -> dict:
        entry = self._children.get(folder_id)
        if entry and (time() - entry[0]) < self.ttl:
            return entry[1]
        names: dict = {}
        page = None
        while True:
            listing = self.client.list_folder(folder_id, page)
            for f in listing.files:
                names[f.name] = f
            page = listing.next_page_token
            if not page:
                break
        self._children[folder_id] = (time(), names)
        return names

    def resolve(self, path: str):
        """Return the RemoteFile at `path`, or None for the root / not found."""
        parts = [p for p in path.split("/") if p]
        node = None
        folder_id = self.root_id
        for part in parts:
            node = self.children(folder_id).get(part)
            if node is None:
                return None
            folder_id = node.id
        return node

    def list_dir(self, path: str) -> list:
        if path in ("", "/"):
            folder_id = self.root_id
        else:
            node = self.resolve(path)
            if node is None or not node.is_folder:
                return []
            folder_id = node.id
        return list(self.children(folder_id).values())

    def cache_path(self, node) -> Path:
        return self.cache_dir / node.id

    def ensure_cached(self, node) -> Path:
        """Download/export the file into the cache on first access."""
        dest = self.cache_path(node)
        if dest.exists():
            return dest
        if node.is_google_doc:
            from electridrive.google_api.client import export_format_for
            mime, _ext = export_format_for(node.mime_type)
            self.client.export_file(node.id, dest, mime)
        else:
            self.client.download_file(node.id, dest)
        return dest


def create_operations_class():
    """Build the fusepy Operations subclass (lazy import of libfuse binding)."""
    import errno
    import stat as statmod

    from fuse import FuseOSError, Operations

    class ElectriDriveFS(Operations):
        def __init__(self, tree: DriveTree, writable: bool = False):
            self.tree = tree
            self.writable = writable
            self._fh = count(1)
            self._open: dict[int, object] = {}

        # ---- read-only metadata ----
        def getattr(self, path, fh=None):
            now = time()
            if path == "/":
                return dict(st_mode=(statmod.S_IFDIR | 0o755), st_nlink=2,
                            st_ctime=now, st_mtime=now, st_atime=now)
            node = self.tree.resolve(path)
            if node is None:
                raise FuseOSError(errno.ENOENT)
            if node.is_folder:
                return dict(st_mode=(statmod.S_IFDIR | 0o755), st_nlink=2,
                            st_ctime=now, st_mtime=now, st_atime=now)
            mode = 0o644 if self.writable else 0o444
            cached = self.tree.cache_path(node)
            size = cached.stat().st_size if cached.exists() else (node.size or 4096)
            mt = _parse_iso(node.modified_time)
            return dict(st_mode=(statmod.S_IFREG | mode), st_nlink=1, st_size=size,
                        st_ctime=mt, st_mtime=mt, st_atime=now)

        def readdir(self, path, fh):
            yield "."
            yield ".."
            for f in self.tree.list_dir(path):
                yield f.name

        def open(self, path, flags):
            node = self.tree.resolve(path)
            if node is None:
                raise FuseOSError(errno.ENOENT)
            cached = self.tree.ensure_cached(node)
            fh = next(self._fh)
            self._open[fh] = open(cached, "rb")
            return fh

        def read(self, path, size, offset, fh):
            handle = self._open.get(fh)
            if handle is None:
                node = self.tree.resolve(path)
                if node is None:
                    raise FuseOSError(errno.ENOENT)
                with open(self.tree.ensure_cached(node), "rb") as fp:
                    fp.seek(offset)
                    return fp.read(size)
            handle.seek(offset)
            return handle.read(size)

        def release(self, path, fh):
            handle = self._open.pop(fh, None)
            if handle:
                handle.close()
            return 0

        # ---- writes denied in the read-only prototype ----
        def _readonly(self, *args, **kwargs):
            raise FuseOSError(errno.EROFS)

        create = unlink = mkdir = rmdir = rename = truncate = write = _readonly

    return ElectriDriveFS


class FuseMount:
    """Manages a background FUSE mount of a Drive folder."""

    def __init__(self, client, cache_dir: Path):
        self.client = client
        self.cache_dir = Path(cache_dir)
        self._thread: threading.Thread | None = None
        self.mountpoint: str | None = None

    @property
    def is_mounted(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, mountpoint: str, remote_folder: str = "", writable: bool = False):
        if self.is_mounted:
            raise RuntimeError("Already mounted")
        if not fuse_available():
            raise RuntimeError("FUSE is not available (need libfuse + fusermount).")
        from fuse import FUSE

        mp = Path(mountpoint).expanduser()
        mp.mkdir(parents=True, exist_ok=True)
        root_id = self.client.ensure_folder_path(remote_folder) if remote_folder else "root"
        tree = DriveTree(self.client, root_id, self.cache_dir)
        fs_cls = create_operations_class()
        self.mountpoint = str(mp)

        def run():
            try:
                FUSE(fs_cls(tree, writable), str(mp), foreground=True,
                     nothreads=True, ro=not writable)
            except Exception:
                LOGGER.exception("FUSE mount exited with error")

        self._thread = threading.Thread(target=run, name="electridrive-fuse", daemon=True)
        self._thread.start()

    def wait(self):
        """Block until the mount exits (for foreground CLI use)."""
        if self._thread:
            self._thread.join()

    def stop(self):
        if not self.mountpoint:
            return
        tool = shutil.which("fusermount3") or shutil.which("fusermount")
        if tool:
            subprocess.run([tool, "-u", self.mountpoint], check=False)
        self._thread = None
        self.mountpoint = None
