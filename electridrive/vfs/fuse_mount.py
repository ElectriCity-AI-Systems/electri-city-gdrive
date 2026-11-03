"""FUSE "Virtual Drive" — mount Google Drive as files-on-demand, without rclone.

The tree/cache logic lives in :class:`DriveTree` (pure, testable with FakeDrive).
The FUSE binding (`fusepy`) is imported lazily inside :func:`create_operations_class`
so importing this module never requires libfuse.

Scope: read + directory listing + on-demand download into a local cache. Google
Workspace files are exported on first read. The mount is intentionally read-only.
"""
from __future__ import annotations

from electridrive import qa_policy as qa

def _deny_qa_fuse():
    if qa.current_policy():
        raise qa.QaSafetyError("QA safety stop: FUSE is disabled")

import hashlib
import os
from collections import Counter
import json
import logging
import shutil
import subprocess
import tempfile
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
        _deny_qa_fuse()
        from electridrive.sync.safety import account_key_for
        self.account_key = account_key_for(client)
        self._cache_lock = threading.RLock()
        self.client = client
        self.root_id = root_id
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._metadata_dir = self.cache_dir / ".metadata"
        self._metadata_dir.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl
        self._children: dict[str, tuple[float, dict]] = {}

    def invalidate(self):
        self._children.clear()

    def children(self, folder_id: str) -> dict:
        entry = self._children.get(folder_id)
        if entry and (time() - entry[0]) < self.ttl:
            return entry[1]
        files = []
        page = None
        seen = set()
        while True:
            listing = self.client.list_folder(folder_id, page)
            files.extend(listing.files)
            page = listing.next_page_token
            if not page:
                break
            if page in seen:
                raise RuntimeError("Drive folder listing returned a repeated page token")
            seen.add(page)
        from electridrive.sync.downloader import sanitize_name
        bases = Counter(sanitize_name(f.name) for f in files)
        names = {}
        for f in sorted(files, key=lambda f: (f.name, f.id)):
            name = sanitize_name(f.name)
            if bases[name] > 1 or name != f.name:
                name += " [" + hashlib.sha256(f.id.encode()).hexdigest()[:12] + "]"
            while name in names:
                name += "_"
            names[name] = f
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

    def _cache_metadata_path(self, node) -> Path:
        return self._metadata_dir / f"{node.id}.json"

    def _cache_identity(self, node) -> dict[str, str] | None:
        """Return the strongest available immutable identity for cached bytes.

        Drive supplies ``md5Checksum`` for ordinary binary files. Google Workspace
        files do not have one, so their ``modifiedTime`` is the safe fallback. A
        missing identity deliberately disables reuse rather than making a cache
        entry permanent.
        """
        if node.md5_checksum:
            identity_type = "md5Checksum"
            identity_value = node.md5_checksum
        elif node.modified_time:
            identity_type = "modifiedTime"
            identity_value = node.modified_time
        else:
            return None

        identity = {
            "identity_type": identity_type,
            "identity_value": identity_value,
            "mime_type": node.mime_type or "",
            "account_key": self.account_key,
            "root_id": self.root_id,
            "file_id": node.id,
            "size": node.size,
        }
        if node.is_google_doc:
            from electridrive.google_api.client import export_format_for

            export_mime, _ext = export_format_for(node.mime_type)
            identity["export_mime"] = export_mime
        return identity

    def _cached_identity(self, node) -> dict[str, str] | None:
        try:
            value = json.loads(self._cache_metadata_path(node).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _digests(path: Path) -> tuple[str, str, int]:
        sha, md5, size = hashlib.sha256(), hashlib.md5(), 0
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                sha.update(chunk)
                md5.update(chunk)
                size += len(chunk)
        return sha.hexdigest(), md5.hexdigest(), size

    def cache_is_current(self, node) -> bool:
        """Validate both scoped remote metadata and streamed cached bytes."""
        with self._cache_lock:
            identity = self._cache_identity(node)
            saved = self._cached_identity(node)
            if identity is None or saved is None or saved.get("remote") != identity:
                return False
            path = self.cache_path(node)
            if path.is_symlink() or not path.is_file():
                return False
            try:
                digest, md5, size = self._digests(path)
            except OSError:
                return False
            return (saved.get("sha256") == digest and saved.get("size") == size
                    and (node.is_google_doc or (
                        (node.size is None or size == node.size)
                        and (not node.md5_checksum or md5 == node.md5_checksum))))

    def _write_cache_identity(self, node, identity: dict[str, str]) -> None:
        metadata_path = self._cache_metadata_path(node)
        metadata_tmp: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self._metadata_dir,
                prefix=f".{node.id}.",
                suffix=".tmp",
                delete=False,
            ) as fp:
                metadata_tmp = Path(fp.name)
                json.dump(identity, fp, sort_keys=True)
            metadata_tmp.replace(metadata_path)
        except Exception:
            if metadata_tmp is not None:
                metadata_tmp.unlink(missing_ok=True)
            raise

    def ensure_cached(self, node) -> Path:
        with self._cache_lock:
            return self._ensure_cached(node)

    def _ensure_cached(self, node) -> Path:
        """Return bytes matching the current remote identity, refreshing if needed."""
        dest = self.cache_path(node)
        identity = self._cache_identity(node)
        if self.cache_is_current(node):
            return dest

        # Download to the cache filesystem and replace atomically. If a network or
        # export error occurs, an older valid cache entry remains intact.
        with tempfile.NamedTemporaryFile(
            dir=self.cache_dir,
            prefix=f".{node.id}.",
            suffix=".tmp",
            delete=False,
        ) as fp:
            download_tmp = Path(fp.name)
        try:
            if node.is_google_doc:
                from electridrive.google_api.client import export_format_for

                mime, _ext = export_format_for(node.mime_type)
                self.client.export_file(node.id, download_tmp, mime)
            else:
                self.client.download_file(node.id, download_tmp)
            digest, md5, size = self._digests(download_tmp)
            if not node.is_google_doc:
                if node.size is not None and size != node.size:
                    raise ValueError("Downloaded cache file has the wrong size")
                if node.md5_checksum and md5 != node.md5_checksum:
                    raise ValueError("Downloaded cache file failed its checksum")
            with download_tmp.open("rb") as stream:
                os.fsync(stream.fileno())
            download_tmp.replace(dest)
            if identity is not None:
                self._write_cache_identity(node, {"remote": identity, "sha256": digest, "size": size})
        except Exception:
            download_tmp.unlink(missing_ok=True)
            raise
        return dest


def create_operations_class():
    """Build the fusepy Operations subclass (lazy import of libfuse binding)."""
    import errno
    import os
    import stat as statmod

    from fuse import FuseOSError, Operations

    class ElectriDriveFS(Operations):
        def __init__(self, tree: DriveTree, writable: bool = False):
            if writable:
                raise ValueError("ElectriDrive Virtual Drive is read-only in version 2.1.2")
            self.tree = tree
            self._fh = count(1)
            self._open: dict[int, object] = {}

        # ---- read-only metadata ----
        def getattr(self, path, fh=None):
            now = time()
            if path == "/":
                return dict(st_mode=(statmod.S_IFDIR | 0o555), st_nlink=2,
                            st_ctime=now, st_mtime=now, st_atime=now)
            node = self.tree.resolve(path)
            if node is None:
                raise FuseOSError(errno.ENOENT)
            if node.is_folder:
                return dict(st_mode=(statmod.S_IFDIR | 0o555), st_nlink=2,
                            st_ctime=now, st_mtime=now, st_atime=now)
            mode = 0o444
            size = (self.tree.ensure_cached(node).stat().st_size if node.is_google_doc
                    else (node.size or 0))
            mt = _parse_iso(node.modified_time)
            return dict(st_mode=(statmod.S_IFREG | mode), st_nlink=1, st_size=size,
                        st_ctime=mt, st_mtime=mt, st_atime=now)

        def readdir(self, path, fh):
            yield "."
            yield ".."
            node = self.tree.resolve(path) if path not in ("", "/") else None
            folder_id = node.id if node else self.tree.root_id
            yield from self.tree.children(folder_id)

        def access(self, path, mode):
            if mode & os.W_OK:
                raise FuseOSError(errno.EROFS)
            if path != "/" and self.tree.resolve(path) is None:
                raise FuseOSError(errno.ENOENT)
            return 0

        def open(self, path, flags):
            if (flags & os.O_ACCMODE) != os.O_RDONLY or flags & (
                os.O_CREAT | os.O_TRUNC | os.O_APPEND
            ):
                raise FuseOSError(errno.EROFS)
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

        # ---- mutation operations are always denied ----
        def _readonly(self, *args, **kwargs):
            raise FuseOSError(errno.EROFS)

        chmod = chown = create = link = mkdir = mknod = removexattr = _readonly
        rename = rmdir = setxattr = symlink = truncate = unlink = utimens = write = _readonly

    return ElectriDriveFS


class FuseMount:
    """Manages a background FUSE mount of a Drive folder."""

    def __init__(self, client, cache_dir: Path):
        _deny_qa_fuse()
        self.client = client
        self.cache_dir = Path(cache_dir)
        self._thread: threading.Thread | None = None
        self.mountpoint: str | None = None
        self._error: Exception | None = None

    @property
    def is_mounted(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, mountpoint: str, remote_folder: str = "", writable: bool = False):
        _deny_qa_fuse()
        if writable:
            raise ValueError("ElectriDrive Virtual Drive is read-only in version 2.1.2")
        if self.is_mounted:
            raise RuntimeError("Already mounted")
        if not fuse_available():
            raise RuntimeError(
                "FUSE is not available (need fusepy, libfuse2/libfuse2t64 and fuse3/fuse)."
            )
        from fuse import FUSE

        mp = Path(mountpoint).expanduser()
        mp.mkdir(parents=True, exist_ok=True)
        root_id = self.client.ensure_folder_path(remote_folder) if remote_folder else "root"
        from electridrive.sync.safety import account_key_for
        key = account_key_for(self.client)
        namespace = hashlib.sha256(key.encode()).hexdigest()
        tree = DriveTree(self.client, root_id, self.cache_dir / namespace)
        fs_cls = create_operations_class()
        self.mountpoint = str(mp)
        self._error = None

        def run():
            try:
                FUSE(fs_cls(tree), str(mp), foreground=True, nothreads=True, ro=True)
            except Exception as exc:
                self._error = exc
                qa.log_exception(LOGGER, "FUSE mount exited with error")

        self._thread = threading.Thread(target=run, name="electridrive-fuse", daemon=True)
        self._thread.start()
        self._thread.join(timeout=0.05)
        if self._error is not None:
            raise RuntimeError(str(self._error)) from self._error

    def wait(self):
        """Block until the mount exits (for foreground CLI use)."""
        if self._thread:
            self._thread.join()
        if self._error is not None:
            raise RuntimeError(str(self._error)) from self._error

    def stop(self):
        if not self.mountpoint:
            return
        tool = shutil.which("fusermount3") or shutil.which("fusermount")
        if not tool:
            raise RuntimeError("No fusermount helper is available")
        result = subprocess.run([tool, "-u", self.mountpoint], check=False,
                                capture_output=True, text=True, timeout=10)
        if result.returncode:
            detail = getattr(result, "stderr", "") or result.returncode
            raise RuntimeError(f"Unmount failed: {detail}")
        if self._thread:
            self._thread.join(timeout=5.0)
            if self._thread.is_alive():
                raise RuntimeError("Unmount did not exit; mount thread is still running")
        self._thread = None
        self.mountpoint = None
