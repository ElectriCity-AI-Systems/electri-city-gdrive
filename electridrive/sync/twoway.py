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
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from electridrive.sync.hasher import md5_file
from electridrive.sync.rules import SyncRules

LOGGER = logging.getLogger(__name__)
LOCAL_TRASH = ".electridrive-trash"


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
                 log_cb=None):
        self.client = client
        self.db = db
        self.pair = pair
        self.rules = rules or SyncRules()
        self.log_cb = log_cb
        self.local_root = Path(pair.local_path).expanduser()
        self.pair_id = pair_id_for(pair.local_path, pair.remote_folder)
        self._dir_ids: dict[str, str] = {}

    def _log(self, msg: str):
        LOGGER.info(msg)
        if self.log_cb:
            self.log_cb(msg)

    # ------------------------------------------------------------- snapshots
    def scan_local(self) -> dict[str, LocalEntry]:
        out: dict[str, LocalEntry] = {}
        root = self.local_root
        if not root.exists():
            return out
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if self.rules.is_excluded(path, root) or LOCAL_TRASH in path.parts:
                continue
            rel = path.relative_to(root).as_posix()
            st = path.stat()
            out[rel] = LocalEntry(rel, st.st_mtime_ns, st.st_size, md5_file(path))
        return out

    def scan_remote(self) -> dict[str, RemoteEntry]:
        root_id = self.client.ensure_folder_path(self.pair.remote_folder)
        self._dir_ids = {"": root_id}
        entries: dict[str, RemoteEntry] = {}
        self._walk_remote(root_id, "", entries)
        return entries

    def _walk_remote(self, folder_id: str, prefix: str, entries: dict[str, RemoteEntry]):
        page = None
        while True:
            listing = self.client.list_folder(folder_id, page)
            for f in listing.files:
                rel = f"{prefix}{f.name}" if not prefix else f"{prefix}/{f.name}"
                if f.is_folder:
                    self._dir_ids[rel] = f.id
                    self._walk_remote(f.id, rel, entries)
                elif f.is_google_doc:
                    continue  # not a real binary file; handled by export elsewhere
                elif f.md5_checksum:
                    entries[rel] = RemoteEntry(rel, f.id, f.modified_time or "",
                                               f.md5_checksum, f.size or 0)
            page = listing.next_page_token
            if not page:
                break

    def load_last(self) -> dict[str, LastEntry]:
        out: dict[str, LastEntry] = {}
        for it in self.db.list_sync_items(self.pair_id):
            out[it.local_rel] = LastEntry(it.local_rel, it.sha256, it.remote_md5)
        return out

    # --------------------------------------------------------------- execute
    def run(self) -> SyncReport:
        report = SyncReport()
        local = self.scan_local()
        remote = self.scan_remote()
        last = self.load_last()
        actions = reconcile(local, remote, last,
                            direction=self.pair.direction,
                            delete_policy=self.pair.delete_policy)
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

    def _apply(self, a: Action, local: dict, remote: dict, report: SyncReport):
        rel = a.rel
        if a.kind == ActionKind.UPLOAD:
            L = local[rel]
            parent_dir = str(Path(rel).parent.as_posix())
            parent_dir = "" if parent_dir == "." else parent_dir
            parent_id = self._ensure_remote_dir(parent_dir)
            fid = self.client.upload_file(self.local_root / rel, parent_id, Path(rel).name)
            meta = self.client.get_metadata(fid)
            remote[rel] = RemoteEntry(rel, fid, meta.modified_time or "",
                                      meta.md5_checksum or L.md5, meta.size or L.size)
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
            self._apply_conflict(a, local, remote, report)
            report.conflicts += 1

        elif a.kind == ActionKind.RECORD:
            self._record(rel, local, remote)
            report.recorded += 1

        elif a.kind == ActionKind.FORGET:
            self.db.delete_sync_item(self.pair_id, rel)

    def _apply_conflict(self, a: Action, local: dict, remote: dict, report: SyncReport):
        rel = a.rel
        L, R = local[rel], remote[rel]
        if a.winner == "local":
            # remote becomes a local conflict copy; local stays canonical and is uploaded
            conflict_rel = _conflict_name(rel)
            self.client.download_file(R.file_id, self.local_root / conflict_rel)
            parent_dir = str(Path(rel).parent.as_posix())
            parent_dir = "" if parent_dir == "." else parent_dir
            parent_id = self._ensure_remote_dir(parent_dir)
            fid = self.client.upload_file(self.local_root / rel, parent_id, Path(rel).name)
            meta = self.client.get_metadata(fid)
            remote[rel] = RemoteEntry(rel, fid, meta.modified_time or "",
                                      meta.md5_checksum or L.md5, meta.size or L.size)
        else:
            # local becomes a conflict copy; remote is downloaded as canonical
            shutil.copy2(self.local_root / rel, self.local_root / _conflict_name(rel))
            dest = self.local_root / rel
            self.client.download_file(R.file_id, dest)
            st = dest.stat()
            local[rel] = LocalEntry(rel, st.st_mtime_ns, st.st_size, md5_file(dest))
        self._record(rel, local, remote)
