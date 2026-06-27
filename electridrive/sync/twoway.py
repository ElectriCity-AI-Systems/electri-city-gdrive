"""Two-way folder sync with a conflict-safe, unit-testable reconciler.

Design: the decision logic (:func:`reconcile`) is a pure function over three
snapshots — local, remote and the last-synced state — so the full case matrix is
testable without any I/O. :class:`TwoWaySyncEngine` executes the resulting plan.

Safety guarantees:
- Deletions are never permanent: a removed remote file goes to **Drive Trash**; a
  removed local file is moved to a local **.electridrive-trash** folder.
- Conflicts never lose data: the newer side wins the canonical name and the other
  version is kept as a clearly-named "(conflict …)" copy.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from electridrive.sync.hasher import md5_file
from electridrive.sync.rules import SyncRules

LOGGER = logging.getLogger(__name__)
LOCAL_TRASH = ".electridrive-trash"

# Serializes the read-token → list_changes → write-token → cache-write drain so two
# concurrent pair syncs (the GUI runs each pair in its own QThreadPool worker) can't
# race the shared, account-wide Drive change feed. Change application is idempotent
# (INSERT OR REPLACE by id), so this only prevents wasted double-fetches / a token
# advancing past unapplied changes.
_DRAIN_LOCK = threading.Lock()


class ActionKind(str, Enum):
    UPLOAD = "upload"
    DOWNLOAD = "download"
    TRASH_REMOTE = "trash_remote"
    TRASH_LOCAL = "trash_local"
    CONFLICT = "conflict"
    RECORD = "record"
    FORGET = "forget"


@dataclass(frozen=True)
class LocalEntry:
    rel: str
    mtime_ns: int
    size: int
    md5: str


@dataclass(frozen=True)
class RemoteEntry:
    rel: str
    file_id: str
    modified: str
    md5: str
    size: int


@dataclass(frozen=True)
class LastEntry:
    rel: str
    local_md5: str
    remote_md5: str


@dataclass(frozen=True)
class Action:
    kind: ActionKind
    rel: str
    winner: str = ""  # for CONFLICT: "local" | "remote"


@dataclass
class SyncReport:
    uploaded: int = 0
    downloaded: int = 0
    trashed_remote: int = 0
    trashed_local: int = 0
    conflicts: int = 0
    recorded: int = 0
    skipped: int = 0
    hashed: int = 0           # local files actually re-hashed this run (diagnostic)
    full_remote_scan: bool = False  # True if the remote view came from a full walk
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def _parse_iso(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _newer(local: LocalEntry, remote: RemoteEntry) -> str:
    return "local" if (local.mtime_ns / 1e9) >= _parse_iso(remote.modified) else "remote"


def reconcile(
    local: dict[str, LocalEntry],
    remote: dict[str, RemoteEntry],
    last: dict[str, LastEntry],
    *,
    direction: str = "two_way",
    delete_policy: str = "trash",
) -> list[Action]:
    """Pure 3-way reconciliation. Returns an ordered list of actions to apply."""
    actions: list[Action] = []
    for rel in sorted(set(local) | set(remote) | set(last)):
        L, R, S = local.get(rel), remote.get(rel), last.get(rel)

        if L and R:
            if L.md5 == R.md5:
                if S is None or S.local_md5 != L.md5 or S.remote_md5 != R.md5:
                    actions.append(Action(ActionKind.RECORD, rel))
                continue
            if S is None:
                actions.append(Action(ActionKind.CONFLICT, rel, _newer(L, R)))
            else:
                local_changed = L.md5 != S.local_md5
                remote_changed = R.md5 != S.remote_md5
                if local_changed and not remote_changed:
                    actions.append(Action(ActionKind.UPLOAD, rel))
                elif remote_changed and not local_changed:
                    actions.append(Action(ActionKind.DOWNLOAD, rel))
                else:
                    actions.append(Action(ActionKind.CONFLICT, rel, _newer(L, R)))

        elif L and not R:
            if S is None:
                actions.append(Action(ActionKind.UPLOAD, rel))           # new local
            elif L.md5 != S.local_md5:
                actions.append(Action(ActionKind.UPLOAD, rel))           # changed; resurrect remote
            elif delete_policy == "off":
                actions.append(Action(ActionKind.FORGET, rel))
            else:
                actions.append(Action(ActionKind.TRASH_LOCAL, rel))      # remote was deleted

        elif R and not L:
            if S is None:
                actions.append(Action(ActionKind.DOWNLOAD, rel))         # new remote
            elif R.md5 != S.remote_md5:
                actions.append(Action(ActionKind.DOWNLOAD, rel))         # changed; resurrect local
            elif delete_policy == "off":
                actions.append(Action(ActionKind.FORGET, rel))
            else:
                actions.append(Action(ActionKind.TRASH_REMOTE, rel))     # local was deleted

        else:  # neither present, but we have history
            actions.append(Action(ActionKind.FORGET, rel))

    return _filter_direction(actions, direction)


def _filter_direction(actions: list[Action], direction: str) -> list[Action]:
    if direction == "two_way":
        return actions
    if direction == "up_only":
        blocked = {ActionKind.DOWNLOAD, ActionKind.TRASH_LOCAL}
    else:  # down_only
        blocked = {ActionKind.UPLOAD, ActionKind.TRASH_REMOTE}
    out = []
    for a in actions:
        if a.kind in blocked:
            continue
        if a.kind == ActionKind.CONFLICT:
            # In one-way mode a conflict collapses to the allowed side.
            if direction == "up_only":
                out.append(Action(ActionKind.UPLOAD, a.rel))
            else:
                out.append(Action(ActionKind.DOWNLOAD, a.rel))
            continue
        out.append(a)
    return out


# --------------------------------------------------------------------------- #
# Executor
# --------------------------------------------------------------------------- #

def pair_id_for(local_path: str, remote_folder: str) -> str:
    return hashlib.sha1(f"{local_path}|{remote_folder}".encode()).hexdigest()[:12]


def _conflict_name(rel: str) -> str:
    p = Path(rel)
    stamp = datetime.now().strftime("%Y-%m-%d %H%M%S")
    return str(p.with_name(f"{p.stem} (conflict {stamp}){p.suffix}"))


class TwoWaySyncEngine:
    def __init__(self, client, db, pair, rules: SyncRules | None = None,
                 log_cb=None, deep_verify: bool = False):
        self.client = client
        self.db = db
        self.pair = pair
        self.rules = rules or SyncRules()
        self.log_cb = log_cb
        # When True, always re-hash local files instead of trusting (mtime, size).
        self.deep_verify = deep_verify
        self.local_root = Path(pair.local_path).expanduser()
        self.pair_id = pair_id_for(pair.local_path, pair.remote_folder)
        self._dir_ids: dict[str, str] = {}
        self._account_key: str | None = None

    def _log(self, msg: str):
        LOGGER.info(msg)
        if self.log_cb:
            self.log_cb(msg)

    # ------------------------------------------------------------- snapshots
    def scan_local(self, report: SyncReport | None = None,
                   baseline: dict[str, tuple[int, int, str]] | None = None
                   ) -> dict[str, LocalEntry]:
        """Snapshot the local tree.

        Enumeration is always complete, so the ``local`` view handed to
        :func:`reconcile` stays authoritative. Files whose ``(mtime_ns, size)`` match
        the last-synced ``baseline`` reuse the stored md5 instead of being re-hashed
        (the reused digest is exactly what reconcile would have computed); the
        remainder are hashed in parallel. ``baseline`` maps ``rel -> (mtime_ns, size,
        md5)``."""
        out: dict[str, LocalEntry] = {}
        root = self.local_root
        if not root.exists():
            return out
        baseline = baseline or {}
        to_hash: list[tuple[str, Path]] = []
        for path in root.rglob("*"):
            try:
                if not path.is_file():
                    continue
                if self.rules.is_excluded(path, root) or LOCAL_TRASH in path.parts:
                    continue
                rel = path.relative_to(root).as_posix()
            except OSError:
                if report is not None:
                    report.skipped += 1
                LOGGER.info("Skipped unreadable local path during scan: %s", path)
                continue
            prev = baseline.get(rel)
            if prev is not None and not self.deep_verify:
                try:
                    st = path.stat()
                except OSError:
                    if report is not None:
                        report.skipped += 1
                    continue
                pmtime, psize, pmd5 = prev
                if st.st_mtime_ns == pmtime and st.st_size == psize:
                    out[rel] = LocalEntry(rel, st.st_mtime_ns, st.st_size, pmd5)
                    continue
            to_hash.append((rel, path))

        for rel, entry in self._hash_files(to_hash):
            if entry is None:
                if report is not None:
                    report.skipped += 1
                LOGGER.info("Skipped changing local file during scan: %s", rel)
            else:
                out[rel] = entry
                if report is not None:
                    report.hashed += 1
        return out

    @staticmethod
    def _hash_one(rel: str, path: Path) -> tuple[str, "LocalEntry | None"]:
        """Hash one file with race protection. Returns ``(rel, entry|None)``; ``None``
        means the file changed or vanished mid-hash and is skipped this run."""
        try:
            before = path.stat()
            digest = md5_file(path)
            after = path.stat()
        except OSError:
            return rel, None
        if before.st_mtime_ns != after.st_mtime_ns or before.st_size != after.st_size:
            return rel, None
        return rel, LocalEntry(rel, after.st_mtime_ns, after.st_size, digest)

    def _hash_files(self, to_hash: list[tuple[str, Path]]
                    ) -> list[tuple[str, "LocalEntry | None"]]:
        """Hash a batch of files, in parallel when there's more than one. Results are
        collected here (main thread) so the report counters are never mutated by
        worker threads."""
        if not to_hash:
            return []
        if len(to_hash) == 1:
            rel, path = to_hash[0]
            return [self._hash_one(rel, path)]
        workers = min(8, (os.cpu_count() or 4), len(to_hash))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(self._hash_one, rel, path) for rel, path in to_hash]
            return [fut.result() for fut in as_completed(futures)]

    def scan_remote(self, seed_cache: bool = False) -> dict[str, RemoteEntry]:
        """Authoritative live walk of the remote subtree. When ``seed_cache`` is set,
        every folder's full child list is written to the remote-node cache so later
        syncs can be served incrementally from change tokens (GUARD 0/1)."""
        root_id = self.client.ensure_folder_path(self.pair.remote_folder)
        self._dir_ids = {"": root_id}
        entries: dict[str, RemoteEntry] = {}
        self._walk_remote(root_id, "", entries, seed_cache)
        return entries

    def _walk_remote(self, folder_id: str, prefix: str,
                     entries: dict[str, RemoteEntry], seed_cache: bool = False):
        children: list = []
        page = None
        while True:
            listing = self.client.list_folder(folder_id, page)
            children.extend(listing.files)
            page = listing.next_page_token
            if not page:
                break
        if seed_cache:
            try:
                self.db.cache_remote_children(folder_id, children)
            except Exception:
                LOGGER.warning("Could not cache remote folder %s", folder_id, exc_info=True)
        for f in children:
            rel = f"{prefix}/{f.name}" if prefix else f.name
            if f.is_folder:
                self._dir_ids[rel] = f.id
                self._walk_remote(f.id, rel, entries, seed_cache)
            elif f.is_google_doc:
                continue  # not a real binary file; handled by export elsewhere
            elif f.md5_checksum:
                entries[rel] = RemoteEntry(rel, f.id, f.modified_time or "",
                                           f.md5_checksum, f.size or 0)

    def load_last(self) -> dict[str, LastEntry]:
        last, _ = self._load_state()
        return last

    def _load_state(self) -> tuple[dict[str, LastEntry], dict[str, tuple[int, int, str]]]:
        """Read the last-synced baseline once, returning both the reconcile ``last``
        view (md5s only) and the ``scan_local`` baseline ``(mtime_ns, size, md5)``."""
        last: dict[str, LastEntry] = {}
        baseline: dict[str, tuple[int, int, str]] = {}
        for it in self.db.list_sync_items(self.pair_id):
            last[it.local_rel] = LastEntry(it.local_rel, it.sha256, it.remote_md5)
            baseline[it.local_rel] = (it.local_mtime_ns, it.local_size, it.sha256)
        return last, baseline

    # --------------------------------------------------------------- execute
    def run(self) -> SyncReport:
        report = SyncReport()
        last, baseline = self._load_state()
        local = self.scan_local(report, baseline)
        remote, full_scanned = self._acquire_remote()
        actions = reconcile(local, remote, last,
                            direction=self.pair.direction,
                            delete_policy=self.pair.delete_policy)
        # GUARD 2 (load-bearing): an optimistic, cache-derived remote view must never
        # drive a local deletion. If the plan would trash anything locally, re-validate
        # against one authoritative full walk and reconcile again before executing.
        if not full_scanned and any(a.kind == ActionKind.TRASH_LOCAL for a in actions):
            self._log("Cache view would trash local files — re-validating with a full remote scan")
            remote = self._full_scan_and_seed(self._get_account_key())
            full_scanned = True
            actions = reconcile(local, remote, last,
                                direction=self.pair.direction,
                                delete_policy=self.pair.delete_policy)
        report.full_remote_scan = full_scanned
        self._log(f"Sync {self.local_root} <-> {self.pair.remote_folder}: "
                  f"{len(actions)} action(s)")
        for a in actions:
            try:
                self._apply(a, local, remote, report)
            except Exception as exc:
                report.failed += 1
                report.errors.append(f"{a.kind.value} {a.rel}: {exc}")
                LOGGER.exception("Sync action failed: %s %s", a.kind.value, a.rel)
        return report

    # ------------------------------------------------------------- remote view
    def _get_account_key(self) -> str:
        """Key for the (account-wide, scope-sensitive) change feed + node cache. Scope
        changes are handled by clearing the cache in the session, so the account email
        is a sufficient key here."""
        if self._account_key is None:
            email = "unknown"
            getter = getattr(self.client, "get_about", None)
            if getter is not None:
                try:
                    email = getattr(getter(), "user_email", None) or "unknown"
                except Exception:
                    LOGGER.debug("get_about failed; using 'unknown' account key", exc_info=True)
            self._account_key = email
        return self._account_key

    def _acquire_remote(self) -> tuple[dict[str, RemoteEntry], bool]:
        """Return ``(remote, full_scanned)``. ``full_scanned`` is True when the view
        came from an authoritative live walk (safe to trust for deletions); False when
        it was reconstructed from the change-token-fed cache."""
        has_changes = (hasattr(self.client, "list_changes")
                       and hasattr(self.client, "get_start_page_token"))
        if not has_changes:
            return self.scan_remote(), True  # legacy client: always a full walk
        account_key = self._get_account_key()
        token = self.db.get_change_token(account_key)
        if not token:                                    # GUARD 0: no token yet
            return self._full_scan_and_seed(account_key), True
        with _DRAIN_LOCK:
            ok = self._drain_changes_into_cache(account_key, token)
        if not ok:                                       # GUARD 0: token invalid/expired
            return self._full_scan_and_seed(account_key), True
        root_id = self.client.ensure_folder_path(self.pair.remote_folder)
        self._dir_ids = {"": root_id}
        remote: dict[str, RemoteEntry] = {}
        self._build_remote_from_cache(root_id, "", remote)   # GUARD 1
        return remote, False

    def _full_scan_and_seed(self, account_key: str) -> dict[str, RemoteEntry]:
        """Authoritative full walk that also (re)seeds the cache and the change token.
        The start token is captured *before* the walk so changes during it are simply
        replayed next time (idempotent)."""
        start = None
        getter = getattr(self.client, "get_start_page_token", None)
        if getter is not None:
            try:
                start = getter()
            except Exception:
                LOGGER.warning("Could not read Drive start page token; "
                               "incremental sync disabled this run", exc_info=True)
        remote = self.scan_remote(seed_cache=True)
        if start:
            try:
                self.db.set_change_token(account_key, start)
            except Exception:
                LOGGER.warning("Could not persist change token", exc_info=True)
        return remote

    def _drain_changes_into_cache(self, account_key: str, token: str) -> bool:
        """Apply every Drive change since ``token`` to the remote-node cache and
        advance the stored token. Returns False if the token is invalid/expired so the
        caller falls back to a full scan."""
        page = token
        new_start: str | None = None
        try:
            while page:
                changes, next_page, new_start_token = self.client.list_changes(page)
                for ch in changes:
                    removed = getattr(ch, "removed", False)
                    f = getattr(ch, "file", None)
                    if removed or (f is not None and getattr(f, "trashed", False)):
                        self.db.delete_remote_node(ch.file_id)
                    elif f is not None:
                        self.db.upsert_remote_node(f)
                if new_start_token:
                    new_start = new_start_token
                page = next_page
        except Exception as exc:
            if self._is_invalid_token_error(exc):
                LOGGER.info("Drive change token invalid/expired; full rescan: %s", exc)
                return False
            raise
        if new_start:
            self.db.set_change_token(account_key, new_start)
        return True

    def _build_remote_from_cache(self, folder_id: str, prefix: str,
                                 entries: dict[str, RemoteEntry]) -> None:
        """Reconstruct the remote subtree view from the cache, applying the exact same
        filters as :meth:`_walk_remote` (recurse folders, skip Google Docs / md5-less
        nodes). Also reseeds ``_dir_ids`` for the upload path."""
        for row in self.db.get_cached_children(folder_id):
            if row["trashed"]:
                continue  # invariant: trashed nodes aren't cached, but never trust one
            name = row["name"]
            rel = f"{prefix}/{name}" if prefix else name
            if row["is_folder"]:
                self._dir_ids[rel] = row["id"]
                self._build_remote_from_cache(row["id"], rel, entries)
            elif row["md5"]:
                entries[rel] = RemoteEntry(rel, row["id"], row["modified_time"] or "",
                                           row["md5"], row["size"] or 0)

    @staticmethod
    def _is_invalid_token_error(exc: Exception) -> bool:
        resp = getattr(exc, "resp", None)
        status = getattr(resp, "status", None)
        if status in (404, 410):
            return True
        return "pagetoken" in str(exc).lower()

    def _ensure_remote_dir(self, rel_dir: str) -> str:
        if rel_dir in self._dir_ids:
            return self._dir_ids[rel_dir]
        parent = ""
        parent_id = self._dir_ids[""]
        for part in rel_dir.split("/"):
            cur = f"{parent}/{part}" if parent else part
            if cur not in self._dir_ids:
                finder = getattr(self.client, "find_folder", None)
                fid = (finder(part, parent_id) if finder else None) or \
                    self.client.create_folder(part, parent_id)
                self._dir_ids[cur] = fid
            parent_id = self._dir_ids[cur]
            parent = cur
        return parent_id

    def _record(self, rel: str, local: dict, remote: dict):
        L, R = local.get(rel), remote.get(rel)
        if not (L and R):
            return
        self.db.upsert_sync_item(
            pair_id=self.pair_id, local_rel=rel, remote_id=R.file_id,
            local_mtime_ns=L.mtime_ns, local_size=L.size, sha256=L.md5,
            remote_modified=R.modified, remote_md5=R.md5)

    def _local_matches(self, rel: str, entry: LocalEntry) -> bool:
        path = self.local_root / rel
        try:
            if not path.is_file():
                return False
            st = path.stat()
        except OSError:
            return False
        return st.st_mtime_ns == entry.mtime_ns and st.st_size == entry.size

    def _remote_entry_from_upload(self, rel: str, local_entry: LocalEntry, parent_id: str) -> RemoteEntry:
        path = self.local_root / rel
        name = Path(rel).name
        uploader = getattr(self.client, "upload_file_with_metadata", None)
        if uploader:
            meta = uploader(path, parent_id, name)
            return RemoteEntry(
                rel,
                meta.id,
                meta.modified_time or "",
                meta.md5_checksum or local_entry.md5,
                meta.size or local_entry.size,
            )

        fid = self.client.upload_file(path, parent_id, name)
        meta = self.client.get_metadata(fid)
        return RemoteEntry(
            rel,
            fid,
            meta.modified_time or "",
            meta.md5_checksum or local_entry.md5,
            meta.size or local_entry.size,
        )

    def _apply(self, a: Action, local: dict, remote: dict, report: SyncReport):
        rel = a.rel
        if a.kind == ActionKind.UPLOAD:
            L = local[rel]
            if not self._local_matches(rel, L):
                report.skipped += 1
                self._log(f"Skipped changed or vanished local file: {rel}")
                return
            parent_dir = str(Path(rel).parent.as_posix())
            parent_dir = "" if parent_dir == "." else parent_dir
            parent_id = self._ensure_remote_dir(parent_dir)
            uploaded = self._remote_entry_from_upload(rel, L, parent_id)
            if not self._local_matches(rel, L):
                report.skipped += 1
                try:
                    self.client.trash(uploaded.file_id)
                except Exception:
                    LOGGER.warning("Could not trash stale uploaded copy: %s", rel, exc_info=True)
                self._log(f"Discarded upload because local file changed during transfer: {rel}")
                return
            remote[rel] = uploaded
            self._record(rel, local, remote)
            report.uploaded += 1

        elif a.kind == ActionKind.DOWNLOAD:
            R = remote[rel]
            dest = self.local_root / rel
            self.client.download_file(R.file_id, dest)
            st = dest.stat()
            local[rel] = LocalEntry(rel, st.st_mtime_ns, st.st_size, md5_file(dest))
            self._record(rel, local, remote)
            report.downloaded += 1

        elif a.kind == ActionKind.TRASH_REMOTE:
            self.client.trash(remote[rel].file_id)
            self.db.delete_sync_item(self.pair_id, rel)
            report.trashed_remote += 1

        elif a.kind == ActionKind.TRASH_LOCAL:
            src = self.local_root / rel
            if src.exists():
                dest = self.local_root / LOCAL_TRASH / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dest))
            self.db.delete_sync_item(self.pair_id, rel)
            report.trashed_local += 1

        elif a.kind == ActionKind.CONFLICT:
            if self._apply_conflict(a, local, remote, report):
                report.conflicts += 1

        elif a.kind == ActionKind.RECORD:
            self._record(rel, local, remote)
            report.recorded += 1

        elif a.kind == ActionKind.FORGET:
            self.db.delete_sync_item(self.pair_id, rel)

    def _apply_conflict(self, a: Action, local: dict, remote: dict, report: SyncReport) -> bool:
        rel = a.rel
        L, R = local[rel], remote[rel]
        if a.winner == "local":
            # remote becomes a local conflict copy; local stays canonical and is uploaded
            conflict_rel = _conflict_name(rel)
            self.client.download_file(R.file_id, self.local_root / conflict_rel)
            parent_dir = str(Path(rel).parent.as_posix())
            parent_dir = "" if parent_dir == "." else parent_dir
            parent_id = self._ensure_remote_dir(parent_dir)
            if not self._local_matches(rel, L):
                report.skipped += 1
                self._log(f"Skipped conflict upload because local file changed: {rel}")
                return False
            uploaded = self._remote_entry_from_upload(rel, L, parent_id)
            if not self._local_matches(rel, L):
                report.skipped += 1
                try:
                    self.client.trash(uploaded.file_id)
                except Exception:
                    LOGGER.warning("Could not trash stale conflict upload: %s", rel, exc_info=True)
                self._log(f"Discarded conflict upload because local file changed during transfer: {rel}")
                return False
            remote[rel] = uploaded
        else:
            # local becomes a conflict copy; remote is downloaded as canonical
            shutil.copy2(self.local_root / rel, self.local_root / _conflict_name(rel))
            dest = self.local_root / rel
            self.client.download_file(R.file_id, dest)
            st = dest.stat()
            local[rel] = LocalEntry(rel, st.st_mtime_ns, st.st_size, md5_file(dest))
        self._record(rel, local, remote)
        return True
