"""Shared conservative filesystem and sync-lifecycle safety helpers."""
from __future__ import annotations

from contextlib import contextmanager
from electridrive import qa_policy as qa
import uuid
import fcntl
import hashlib
import os
import stat
from pathlib import Path
import tempfile


class SyncCancelled(RuntimeError):
    pass


def check_cancelled(event) -> None:
    if event is not None and event.is_set():
        raise SyncCancelled('Sync cancelled')


def account_key_for(client) -> str:
    policy = qa.current_policy()
    if policy:
        policy.verify()
        return 'qa:' + str(policy.profile) + ':' + policy.nonce
    key = getattr(client, 'account_key', None)
    if key:
        return str(key)
    # Older adapters can expose account information without implementing the new
    # property. Unknown identities must never share persistent deletion history.
    about = client.get_about() if hasattr(client, 'get_about') else None
    email = getattr(about, 'user_email', None)
    if email:
        return 'email:' + email.casefold()
    # Minimal legacy adapters get only process-local state, never shared history.
    key = getattr(client, '_sync_instance_key', None)
    if key is None:
        key = 'ephemeral:' + uuid.uuid4().hex
        client._sync_instance_key = key
    return key


def validate_component(name: str) -> str:
    if not name or name in {'.', '..'} or any(c in name for c in ('/', '\\', '\x00')):
        raise ValueError(f'Unsafe remote name: {name!r}')
    return name


def checked_path(root: Path, rel: str) -> Path:
    parts = rel.split('/')
    for part in parts:
        validate_component(part)
    path = root
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise ValueError(f'Symbolic links are excluded: {path}')
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f'Path escapes sync root: {rel!r}')
    return path


def has_symlink(root: Path, path: Path) -> bool:
    current = path
    while current != root:
        if current.is_symlink():
            return True
        if current.parent == current:
            return True
        current = current.parent
    return root.is_symlink()


@contextmanager
def pair_lock(local_root: Path):
    """Serialize processes as well as threads, even across different databases."""
    policy = qa.current_policy()
    if policy:
        policy.verify()
        directory = policy.profile / 'state/electridrive'
    else:
        directory = Path(tempfile.gettempdir()) / f'electridrive-locks-{os.getuid()}'
        directory.mkdir(mode=0o700, exist_ok=True)
    if (directory.is_symlink() or directory.stat().st_uid != os.getuid()
            or directory.stat().st_mode & 0o077):
        raise ValueError('Unsafe sync lock directory')
    name = 'sync-' + hashlib.sha256(str(local_root.absolute()).encode()).hexdigest()
    fd = os.open(directory / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        details = os.fstat(fd)
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1 or details.st_uid != os.getuid():
            raise ValueError('Unsafe sync lock file')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('A sync is already running for this local root') from exc
        yield
    finally:
        os.close(fd)


@contextmanager
def anchored_parent(root: Path, rel: str, *, create: bool = False):
    """Pin each directory without following links, including during a rename race."""
    policy = qa.current_policy()
    if policy:
        target = policy.local(root / rel, exists=False)
        with policy.directory_fd(target.parent, create=create) as parent_fd:
            yield parent_fd, target.name
        return
    qa.require_local(root / rel, exists=False)
    checked_path(root, rel)
    parts = rel.split('/')
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass  # idempotent: the directory only needs to exist
            child_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child_fd
        yield fd, parts[-1]
    finally:
        os.close(fd)


@contextmanager
def opened_source(root: Path, rel: str):
    """Expose an already-open regular file to path-based Drive adapters on Linux."""
    if qa.current_policy():
        # The restricted QA adapter pins its own source and rejects proc paths.
        yield qa.require_local(root / rel)
        return
    with anchored_parent(root, rel) as (parent_fd, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError(f'Not a regular source file: {rel}')
            yield Path(f'/proc/self/fd/{fd}')
        finally:
            os.close(fd)
