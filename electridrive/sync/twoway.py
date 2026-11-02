"""Two-way folder sync with a conflict-safe, unit-testable reconciler.

Design: the decision logic (:func:`reconcile`) is a pure function over three
snapshots — local, remote and the last-synced state — so the full case matrix is
testable without any I/O. :class:`TwoWaySyncEngine` executes the resulting plan.

Safety guarantees:
- Deletions are never permanent: deleting locally moves the remote counterpart to
  **Drive Trash**; deleting remotely moves the local counterpart to
  **.electridrive-trash**.
- Conflicts never lose data: the newer side wins the canonical name and the other
  version is kept as a clearly-named "(conflict …)" copy.
- Native Google Workspace files are exported locally and never media-updated from
  their Office-format exports.
"""
from __future__ import annotations

from electridrive import qa_policy as qa

import hashlib
import logging
import json
import os
import tempfile
import uuid
import shutil
from collections import Counter
from dataclasses import dataclass, field, replace, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path

from electridrive.google_api.client import DRIVE_SHORTCUT_MIME, export_format_for
from electridrive.sync.downloader import sanitize_name
from electridrive.sync.hasher import md5_file
from electridrive.sync.rules import SyncRules
from electridrive.sync.tree import canonical_sync_root, safe_local_path, scan_local_tree

from electridrive.sync.safety import (SyncCancelled, check_cancelled, account_key_for, pair_lock, opened_source, anchored_parent, checked_path, has_symlink, validate_component)

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
    is_google_doc: bool = False
    export_mime: str | None = None
    mime_type: str | None = None
    web_view_link: str | None = None


@dataclass(frozen=True)
class RemotePathCandidate:
    file_id: str
    is_folder: bool
    size: int | None
    modified: str
    mime_type: str | None = None
    web_view_link: str | None = None


@dataclass(frozen=True)
class RemotePathConflict:
    remote_path: str
    direction: str
    reason: str
    candidates: tuple[RemotePathCandidate, ...]
    canonical_id: str = ""
    resolved: bool = False
    blocks_sync: bool = True


@dataclass(frozen=True)
class LastEntry:
    rel: str
    local_md5: str
    remote_md5: str
    remote_id: str = ""


@dataclass(frozen=True)
class Action:
    kind: ActionKind
    rel: str
    winner: str = ""  # for CONFLICT: "local" | "remote"


@dataclass
class SyncReport:
    scanned_local_files: int = 0
    scanned_remote_files: int = 0
    scanned_local_folders: int = 0
    scanned_remote_folders: int = 0
    excluded_local: int = 0
    excluded_remote: int = 0
    uploaded: int = 0
    downloaded: int = 0
    folders_created_remote: int = 0
    folders_created_local: int = 0
    folders_reused: int = 0
    trashed_remote: int = 0
    trashed_local: int = 0
    conflicts: int = 0
    recorded: int = 0
    failed: int = 0
    unexplained_omissions: int = 0
    errors: list[str] = field(default_factory=list)
    remote_path_conflicts: list[RemotePathConflict] = field(default_factory=list)
    blocked_before_transfers: bool = False


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

        if S and R and S.remote_id and S.remote_id != R.file_id:
            S = None

        # Native Workspace files are deliberately remote-authoritative exports.
        # Their exported bytes have no meaningful equality relationship with a
        # Drive md5Checksum (which native files do not expose), so compare each
        # side only with its own last-synced fingerprint.  Never media-update a
        # native Doc/Sheet/Slide from its local Office/PDF export.
        if R and R.is_google_doc:
            if direction == "up_only":
                if L is not None:
                    # Preserve the native object and create/update a separate
                    # binary Drive object for the upload-only local file.
                    actions.append(Action(ActionKind.UPLOAD, rel))
                continue
            if L is None:
                # This also restores an intentionally/accidentally deleted local
                # export instead of propagating that deletion to the native file.
                actions.append(Action(ActionKind.DOWNLOAD, rel))
            elif S is None:
                actions.append(Action(ActionKind.CONFLICT, rel, "workspace_remote"))
            else:
                local_changed = L.md5 != S.local_md5
                remote_changed = R.md5 != S.remote_md5
                if local_changed:
                    actions.append(Action(ActionKind.CONFLICT, rel, "workspace_remote"))
                elif remote_changed:
                    actions.append(Action(ActionKind.DOWNLOAD, rel))
            continue

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

    return _filter_direction(actions, direction, local=local, remote=remote)


def _filter_direction(
    actions: list[Action],
    direction: str,
    *,
    local: dict[str, LocalEntry] | None = None,
    remote: dict[str, RemoteEntry] | None = None,
) -> list[Action]:
    if direction == "two_way":
        return actions
    local = local or {}
    remote = remote or {}
    out = []
    for a in actions:
        if a.kind == ActionKind.CONFLICT:
            if a.winner == "workspace_remote":
                # Preserve locally edited exports even in down-only mode; they
                # cannot safely be written back into a native Workspace file.
                if direction != "up_only":
                    out.append(a)
                continue
            # In one-way mode a conflict collapses to the allowed side.
            if direction == "up_only":
                out.append(Action(ActionKind.UPLOAD, a.rel))
            else:
                out.append(Action(ActionKind.DOWNLOAD, a.rel))
            continue

        if direction == "up_only":
            if a.kind == ActionKind.DOWNLOAD:
                # A remote-only file is outside upload-only authority. If both
                # sides exist, local is authoritative and must overwrite it.
                if a.rel in local:
                    out.append(Action(ActionKind.UPLOAD, a.rel))
                continue
            if a.kind == ActionKind.TRASH_LOCAL:
                # A remote deletion must not erase upload-only source data.
                out.append(Action(ActionKind.UPLOAD, a.rel))
                continue
            if a.kind == ActionKind.TRASH_REMOTE:
                # Upload-only never propagates local deletions.
                continue
        else:  # down_only
            if a.kind == ActionKind.UPLOAD:
                # A local-only file is outside download-only authority. If both
                # sides exist, restore the authoritative remote version.
                if a.rel in remote:
                    out.append(Action(ActionKind.DOWNLOAD, a.rel))
                continue
            if a.kind == ActionKind.TRASH_REMOTE:
                # A local deletion must not remove download-only source data.
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
                 log_cb=None, cancel_event=None):
        qa.check_startup()
        qa.validate_pair(pair)
        self.client = qa.restrict_client(client)
        self.db = db
        self.pair = pair
        # Full backup is the default for configured sync pairs, including
        # existing settings saved before include_all_files was introduced.
        # Explicit rule objects still take precedence for expert callers.
        if rules is not None:
            self.rules = rules
        elif pair.include_all_files:
            full_rules = SyncRules.full_backup()
            # Upload-only backs up even a pre-existing .electridrive-trash
            # directory. In two-way/download modes the app's own recoverable
            # trash must not be mirrored back as newly created Drive data:
            # that would reintroduce entries just moved to Trash.
            if pair.direction != "up_only":
                full_rules = SyncRules(
                    include_hidden=True,
                    excluded_names={LOCAL_TRASH},
                    excluded_suffixes=set(),
                    max_file_size_bytes=None,
                    fail_on_unsupported=True,
                )
            self.rules = full_rules
        else:
            self.rules = SyncRules()
        self.cancel_event = cancel_event
        self._root_identity = None
        self._bound_root_id = None
        self._snapshot_key = None
        self._suppressed = set()
        self._local_exclusions = set()
        self.log_cb = log_cb
        self.local_root = Path(pair.local_path).expanduser()
        self._account_key = account_key_for(self.client)
        self._scope = json.dumps([self._account_key, str(self.local_root.absolute()), pair.remote_folder])
        self._binding_key = 'sync-v2:' + hashlib.sha256(self._scope.encode()).hexdigest()
        self.pair_id = 'v2:' + hashlib.sha256(self._scope.encode()).hexdigest()
        self.change_token_key = f"two-way:{self.pair_id}"
        self._dir_ids: dict[str, str] = {}
        self._canonical_ids: dict[str, str] = {}
        self._canonical_paths: dict[str, str] = {}
        self._remote_snapshot: dict[str, RemoteEntry] | None = None
        self._local_dirs: set[str] = set()
        self._remote_dirs: set[str] = set()
        self._local_scan_errors: list[str] = []
        self._remote_scan_errors: list[str] = []
        # Drive allows multiple distinct file IDs under the same folder/name.
        # Retain all IDs so upload-only can reject ambiguous write targets.
        self._duplicate_remote_ids: dict[str, set[str]] = {}
        self._path_conflict_candidates: dict[
            str, dict[str, RemotePathCandidate]
        ] = {}
        self._path_conflict_reasons: dict[str, str] = {}
        self._dir_candidates: dict[str, RemotePathCandidate] = {}
        self._duplicate_file_paths: set[str] = set()
        self._local_excluded = 0
        self._remote_excluded = 0

    def _log(self, msg: str):
        LOGGER.info(msg)
        if self.log_cb:
            self.log_cb(msg)

    # ------------------------------------------------------------- snapshots
    def scan_local(self) -> dict[str, LocalEntry]:
        qa.check_startup()
        qa.validate_pair(self.pair)
        out: dict[str, LocalEntry] = {}
        tree = scan_local_tree(self.local_root, self.rules)
        self.local_root = tree.root
        self._local_dirs = tree.relative_directories
        self._local_scan_errors = list(tree.errors)
        self._local_excluded = len(tree.excluded)
        self._local_exclusions = set(tree.excluded)
        for path in tree.files:
            rel = path.relative_to(tree.root).as_posix()
            try:
                self._check_cancel()
                path = safe_local_path(tree.root, rel)
                if not path.is_file():
                    raise OSError("selected entry is no longer a regular file")
                with opened_source(tree.root, rel) as source:
                    st = source.stat()
                    digest = md5_file(source)
                    after = source.stat()
                    if (st.st_size, st.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                        raise OSError("local file changed while scanning")
                out[rel] = LocalEntry(rel, st.st_mtime_ns, st.st_size, digest)
            except OSError as exc:
                self._local_scan_errors.append(
                    f"cannot read {rel}: {type(exc).__name__}"
                )
        return out

    def scan_remote(self) -> dict[str, RemoteEntry]:
        qa.check_startup()
        qa.validate_pair(self.pair)
        self.client = qa.restrict_client(self.client)
        # A failed root lookup must not expose candidates from a previous run.
        self._dir_ids = {}
        self._remote_dirs = set()
        self._remote_scan_errors = []
        self._duplicate_remote_ids = {}
        self._path_conflict_candidates = {}
        self._path_conflict_reasons = {}
        self._dir_candidates = {}
        self._duplicate_file_paths = set()
        self._remote_excluded = 0
        self._suppressed = set(self._local_exclusions)
        root_id = self._bound_root_id or self._bind_roots()
        self._dir_ids = {"": root_id}
        entries: dict[str, RemoteEntry] = {}
        self._walk_remote(root_id, "", entries, {root_id})
        return entries

    def _walk_remote(
        self,
        folder_id: str,
        prefix: str,
        entries: dict[str, RemoteEntry],
        visited: set[str],
    ):
        page = None
        seen_pages: set[str] = set()
        while True:
            self._check_cancel()
            listing = self.client.list_folder(folder_id, page)
            for f in listing.files:
                name = sanitize_name(f.name)
                export_mime = None
                is_shortcut = f.mime_type == DRIVE_SHORTCUT_MIME
                if f.is_google_doc and not is_shortcut:
                    export_mime, ext = export_format_for(f.mime_type)
                    if not name.lower().endswith(ext):
                        name = f"{name}{ext}"
                rel = f"{prefix}{name}" if not prefix else f"{prefix}/{name}"
                if self._excluded(rel):
                    self._suppressed.add(rel)
                    self._remote_excluded += 1
                    continue
                if f.is_folder:
                    if self.rules.is_excluded_relative(rel, is_file=False):
                        self._remote_excluded += 1
                        continue
                    candidate = self._candidate_from_file(f)
                    if rel in self._dir_ids or rel in entries:
                        existing = self._dir_candidates.get(rel)
                        if existing:
                            self._record_path_collision(
                                rel, "Multiple Drive folders share this path.",
                                existing, candidate,
                            )
                        elif rel in entries:
                            self._record_path_collision(
                                rel, "A Drive file and folder share this path.",
                                self._candidate_from_entry(entries[rel]), candidate,
                            )
                        self._remote_scan_errors.append("remote path collision")
                        continue
                    if f.id in visited:
                        self._record_path_collision(
                            rel, "Drive folder traversal contains a cycle.",
                            candidate,
                        )
                        self._remote_scan_errors.append("remote folder cycle")
                        continue
                    self._dir_ids[rel] = f.id
                    self._dir_candidates[rel] = candidate
                    self._remote_dirs.add(rel)
                    visited.add(f.id)
                    self._walk_remote(f.id, rel, entries, visited)
                elif self.rules.is_excluded_relative(
                    rel, is_file=True, size=f.size
                ) or is_shortcut:
                    self._suppressed.add(rel)
                    self._remote_excluded += 1
                elif f.is_google_doc:
                    fingerprint = f"workspace:{f.id}:{f.modified_time or ''}"
                    self._add_remote_entry(
                        entries,
                        RemoteEntry(
                            rel,
                            f.id,
                            f.modified_time or "",
                            fingerprint,
                            f.size or 0,
                            is_google_doc=True,
                            export_mime=export_mime,
                            mime_type=f.mime_type,
                            web_view_link=f.web_view_link,
                        ),
                    )
                elif f.md5_checksum:
                    self._add_remote_entry(
                        entries,
                        RemoteEntry(
                            rel,
                            f.id,
                            f.modified_time or "",
                            f.md5_checksum,
                            f.size or 0,
                            mime_type=f.mime_type,
                            web_view_link=f.web_view_link,
                        ),
                    )
                else:
                    self._remote_scan_errors.append(
                        "remote file has no synchronizable content fingerprint"
                    )
            page = listing.next_page_token
            if not page:
                break
            if page in seen_pages:
                raise RuntimeError("Drive folder listing returned a repeated page token")
            seen_pages.add(page)

    def _add_remote_entry(
        self, entries: dict[str, RemoteEntry], candidate: RemoteEntry
    ) -> None:
        """Add an entry without hiding a Workspace/binary path collision.

        Drive permits a native ``Notes`` and a binary ``Notes.docx`` in the same
        folder.  Both map naturally to ``Notes.docx`` locally.  An established
        database path wins; otherwise the real binary keeps its literal name and
        the native export receives a deterministic type suffix.
        """
        existing = entries.get(candidate.rel)
        if candidate.rel in self._dir_ids:
            self._record_path_collision(
                candidate.rel, "A Drive file and folder share this path.",
                self._dir_candidates[candidate.rel],
                self._candidate_from_entry(candidate),
            )
            self._remote_scan_errors.append("remote path collision")
            return
        if existing is None:
            entries[candidate.rel] = candidate
            return

        preferred = self._canonical_ids.get(candidate.rel)
        cross_type_collision = existing.is_google_doc != candidate.is_google_doc
        if not cross_type_collision:
            if existing.file_id == candidate.file_id:
                # Repeated paginated API item; one Drive object, not two.
                entries[candidate.rel] = candidate
                return
            self._record_path_collision(
                candidate.rel, "Multiple Drive files share this path.",
                self._candidate_from_entry(existing),
                self._candidate_from_entry(candidate),
            )
            self._duplicate_file_paths.add(candidate.rel)
            if self.pair.direction == "up_only":
                # Remote-only objects will never be downloaded, updated or
                # trashed in upload-only mode. Track ambiguous IDs rather than
                # blocking unrelated local uploads. Validate every local upload
                # target *after* the entire remote traversal is complete,
                # including when a Changes API cache is reused.
                duplicates = self._duplicate_remote_ids.setdefault(
                    candidate.rel, {existing.file_id}
                )
                duplicates.add(candidate.file_id)
                if preferred == candidate.file_id:
                    entries[candidate.rel] = candidate
                return
            if preferred == candidate.file_id:
                entries[candidate.rel] = candidate
            return

        if preferred == existing.file_id:
            alternate = self._collision_rel(candidate, entries)
            entries[alternate] = replace(candidate, rel=alternate)
        elif preferred == candidate.file_id:
            alternate = self._collision_rel(existing, entries)
            entries.pop(candidate.rel)
            entries[alternate] = replace(existing, rel=alternate)
            entries[candidate.rel] = candidate
        elif existing.is_google_doc:
            alternate = self._collision_rel(existing, entries)
            entries.pop(candidate.rel)
            entries[alternate] = replace(existing, rel=alternate)
            entries[candidate.rel] = candidate
        else:
            alternate = self._collision_rel(candidate, entries)
            entries[alternate] = replace(candidate, rel=alternate)

    @staticmethod
    def _candidate_from_file(file) -> RemotePathCandidate:
        return RemotePathCandidate(
            file_id=file.id,
            is_folder=file.is_folder,
            size=file.size,
            modified=file.modified_time or "",
            mime_type=file.mime_type,
            web_view_link=file.web_view_link,
        )

    @staticmethod
    def _candidate_from_entry(entry: RemoteEntry) -> RemotePathCandidate:
        return RemotePathCandidate(
            file_id=entry.file_id,
            is_folder=False,
            size=entry.size,
            modified=entry.modified,
            mime_type=entry.mime_type,
            web_view_link=entry.web_view_link,
        )

    def _record_path_collision(
        self,
        rel: str,
        reason: str,
        *candidates: RemotePathCandidate,
    ) -> None:
        self._path_conflict_reasons[rel] = reason
        by_id = self._path_conflict_candidates.setdefault(rel, {})
        for candidate in candidates:
            by_id[candidate.file_id] = candidate

    def _path_conflicts(
        self, remote: dict[str, RemoteEntry], local_paths: set[str],
    ) -> list[RemotePathConflict]:
        conflicts = []
        for rel, candidates in sorted(self._path_conflict_candidates.items()):
            ids = set(candidates)
            canonical_id = self._canonical_ids.get(rel, "")
            resolved = (
                len(ids) > 1
                and all(not item.is_folder for item in candidates.values())
                and canonical_id in ids
                and remote.get(rel) is not None
                and remote[rel].file_id == canonical_id
            )
            conflicts.append(
                RemotePathConflict(
                    remote_path=rel,
                    direction=self.pair.direction,
                    reason=self._path_conflict_reasons[rel],
                    candidates=tuple(candidates[key] for key in sorted(candidates)),
                    canonical_id=canonical_id if resolved else "",
                    resolved=resolved,
                    blocks_sync=not resolved and not (
                        self.pair.direction == "up_only"
                        and rel in self._duplicate_file_paths
                        and rel not in local_paths
                        and rel not in self.pair.canonical_remote_ids
                        and all(not item.is_folder for item in candidates.values())
                    ),
                )
            )
        return conflicts
    def _collision_rel(
        self, entry: RemoteEntry, entries: dict[str, RemoteEntry]
    ) -> str:
        occupied = set(entries) | set(self._dir_ids)
        previous = self._canonical_paths.get(entry.file_id)
        if previous and previous != entry.rel and previous not in occupied:
            return previous

        path = Path(entry.rel)
        if entry.is_google_doc:
            labels = {
                ".docx": "Google Doc",
                ".xlsx": "Google Sheet",
                ".pptx": "Google Slides",
                ".png": "Google Drawing",
            }
            label = labels.get(path.suffix.lower(), "Google Workspace")
        else:
            label = "Drive file"
        alternate = str(path.with_name(f"{path.stem} ({label}){path.suffix}"))
        if alternate != entry.rel and alternate not in occupied:
            return alternate

        # A literal file may itself use the type-suffixed name.  The immutable
        # Drive ID keeps this final fallback stable and collision-free.
        short_id = entry.file_id[:8]
        alternate = str(
            path.with_name(f"{path.stem} ({label} {short_id}){path.suffix}")
        )
        index = 2
        while alternate in occupied:
            alternate = str(
                path.with_name(
                    f"{path.stem} ({label} {short_id} {index}){path.suffix}"
                )
            )
            index += 1
        return alternate

    def load_last(self) -> dict[str, LastEntry]:
        out: dict[str, LastEntry] = {}
        for it in self.db.list_sync_items(self.pair_id):
            if self._excluded(it.local_rel) or self.rules.is_excluded_relative(it.local_rel, is_file=True, size=it.local_size):
                continue
            out[it.local_rel] = LastEntry(
                it.local_rel, it.sha256, it.remote_md5, it.remote_id
            )
        return out

    def _check_cancel(self, *_args):
        check_cancelled(self.cancel_event)

    def _check_root(self):
        requested = Path(self.pair.local_path).expanduser()
        if requested.is_symlink() or any(parent.is_symlink() for parent in requested.parents):
            raise ValueError("Sync root cannot be a symbolic link")
        stat = requested.stat()
        identity = [stat.st_dev, stat.st_ino]
        if not requested.is_dir() or (self._root_identity is not None and identity != self._root_identity):
            raise ValueError("Local sync root identity changed; explicit rebind required")
        return identity

    def _excluded(self, rel: str) -> bool:
        return (any(rel == item or rel.startswith(item + '/') for item in self._suppressed | self._local_exclusions)
                or self.rules.is_excluded_relative(rel, is_file=True)
                or has_symlink(self.local_root, self.local_root / rel))

    def _bind_roots(self) -> str:
        self._check_cancel()
        identity = self._check_root()
        saved = self.db.get_meta(self._binding_key)
        if saved:
            binding = json.loads(saved)
            if binding['local_identity'] != identity:
                raise ValueError("Local sync root identity changed; explicit rebind required")
            root_id = binding['remote_id']
            metadata = self.client.get_metadata(root_id)
            if metadata.trashed or not metadata.is_folder:
                raise ValueError("Bound remote sync root unavailable; explicit rebind required")
        else:
            if qa.current_policy():
                root_id = self.client.ensure_folder_path(self.pair.remote_folder)
            elif self.pair.direction == "down_only":
                # Resolving a download source must be read-only. A typo, moved
                # folder or revoked access is not authority to create cloud data.
                root_id = "root"
                parts = self.pair.remote_folder.replace("\\", "/").split("/")
                for part in (part.strip() for part in parts if part.strip()):
                    found = self.client.find_folder(part, root_id)
                    if found is None:
                        raise ValueError("Download source Drive folder is missing or inaccessible")
                    root_id = found
            else:
                root_id = self.client.ensure_folder_path(self.pair.remote_folder)
            binding = {'local_identity': identity, 'remote_id': root_id}
        # A new relationship never inherits deletion authority, even if its
        # paths/remote ID are unchanged or the filesystem reuses an inode.
        # Fieldless legacy bindings are verified above, then baseline afresh.
        if not isinstance(binding.get('generation'), str) or not binding['generation']:
            binding['generation'] = uuid.uuid4().hex
            self.db.set_meta(self._binding_key, json.dumps(binding))
        self._root_identity = identity
        self._bound_root_id = root_id
        relationship = json.dumps([self._scope, identity, root_id, binding['generation']])
        self.pair_id = 'v3:' + hashlib.sha256(relationship.encode()).hexdigest()
        return root_id

    # ---------------------------------------------------------- Changes API
    def _consume_changes(self, page_token: str) -> tuple[bool, str]:
        """Drain a Changes feed and return (had_changes, resume_token)."""
        page = page_token
        seen_pages = {page_token}
        had_changes = False
        while True:
            self._check_cancel()
            changes, next_page, new_start = self.client.list_changes(page)
            had_changes = had_changes or bool(changes)
            if next_page:
                if next_page in seen_pages:
                    raise RuntimeError("Drive Changes API returned a repeated page token")
                seen_pages.add(next_page)
                page = next_page
                continue
            if not new_start:
                raise RuntimeError("Drive Changes API did not return a resume token")
            return had_changes, new_start

    def _remote_for_run(self) -> dict[str, RemoteEntry]:
        root_id = self._bind_roots()
        if qa.current_policy():
            # QA capability boundaries forbid account-global Changes requests.
            return self.scan_remote()
        fingerprint = json.dumps([self.pair_id, root_id, asdict(self.rules), sorted(self._local_exclusions), self.pair.canonical_remote_ids], sort_keys=True, default=lambda value: sorted(value))
        self._snapshot_key = 'remote-snapshot-v2:' + hashlib.sha256(fingerprint.encode()).hexdigest()
        cached = self._read_remote_snapshot(root_id)
        token = self.db.get_change_token(self.change_token_key)
        if token and callable(getattr(self.client, 'list_changes', None)):
            try:
                changed, resume = self._consume_changes(token)
                if cached is not None and cached['token'] == token and not changed:
                    self._dir_ids = cached['directories']
                    self._remote_dirs = set(self._dir_ids) - {''}
                    self._suppressed = set(cached['suppressed'])
                    self._remote_scan_errors = []
                    entries = {item['rel']: RemoteEntry(**item) for item in cached['entries']}
                    self._write_remote_snapshot(entries, resume)
                    self.db.set_change_token(self.change_token_key, resume)
                    return entries
            except SyncCancelled:
                raise
            except Exception:
                pass
        self._invalidate_remote_snapshot()
        observed_change = False
        for _attempt in range(3):
            self._check_cancel()
            baseline = None
            try:
                starter = getattr(self.client, 'get_start_page_token', None)
                baseline = starter() if callable(starter) else None
            except SyncCancelled:
                raise
            except Exception:
                pass
            if observed_change and not baseline:
                raise RuntimeError("Remote inventory changed; cannot verify a stable rescan")
            entries = self.scan_remote()
            if not baseline or self._remote_scan_errors:
                return entries
            try:
                changed_during_scan, _resume = self._consume_changes(baseline)
            except SyncCancelled:
                raise
            except Exception as exc:
                if observed_change:
                    raise RuntimeError("Remote inventory changed; cannot verify a stable rescan") from exc
                # No observed concurrent mutation: preserve the full-inventory
                # fallback for unavailable Changes endpoints, without caching.
                return entries
            if changed_during_scan:
                # A known stale inventory must never reach reconciliation. A
                # fresh pre-scan cursor bounds every retry; continued activity
                # aborts before actions rather than exhausting unbounded scans.
                observed_change = True
                continue
            self.db.set_change_token(self.change_token_key, baseline)
            self._write_remote_snapshot(entries, baseline)
            return entries
        raise RuntimeError("Remote inventory kept changing during scan; retry synchronization")

    def _read_remote_snapshot(self, root_id: str) -> dict | None:
        raw = self.db.get_meta(self._snapshot_key)
        if not raw:
            return None
        try:
            envelope = json.loads(raw)
            cached = envelope['snapshot']
            encoded = json.dumps(cached, sort_keys=True).encode()
            if hashlib.sha256(encoded).hexdigest() != envelope['checksum']:
                raise ValueError('Snapshot checksum mismatch')
            if (cached['version'] != 1 or not isinstance(cached['token'], str) or
                    not cached['token'] or cached['directories'].get('') != root_id):
                raise ValueError('Invalid snapshot identity or cursor')
            seen = set()
            for item in cached['entries']:
                entry = RemoteEntry(**item)
                for part in entry.rel.split('/'):
                    validate_component(part)
                if entry.rel in seen or not entry.file_id or not entry.md5:
                    raise ValueError('Invalid snapshot file entry')
                seen.add(entry.rel)
            for rel in set(cached['directories']) | set(cached['suppressed']):
                if rel:
                    for part in rel.split('/'):
                        validate_component(part)
            return cached
        except (ValueError, KeyError, TypeError):
            LOGGER.warning('Invalid remote snapshot; taking a full inventory')
            return None

    def _unchanged_token(self, token: str) -> str | None:
        seen = set()
        while token:
            self._check_cancel()
            if token in seen or len(seen) >= 10000:
                raise ValueError('Invalid Changes API pagination')
            seen.add(token)
            changes, next_page, new_start = self.client.list_changes(token)
            if changes:
                return None
            if not next_page:
                return new_start or None
            token = next_page
        return None

    def _write_remote_snapshot(self, entries: dict[str, RemoteEntry], token: str | None) -> None:
        if self._remote_scan_errors or self._path_conflict_candidates or not token:
            return
        snapshot = {
            'version': 1,
            'token': token,
            'entries': [asdict(entries[rel]) for rel in sorted(entries)],
            'directories': self._dir_ids,
            'suppressed': sorted(self._suppressed),
        }
        checksum = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
        self.db.set_meta(self._snapshot_key, json.dumps({'snapshot': snapshot, 'checksum': checksum}))

    def _invalidate_remote_snapshot(self) -> None:
        # Invalidate before our own writes: even an interrupted mutation must not
        # leave a pre-write inventory eligible for no-change reuse.
        if self._snapshot_key:
            self.db.set_meta(self._snapshot_key, '')

    # --------------------------------------------------------------- execute
    def run(self) -> SyncReport:
        self._check_cancel()
        with pair_lock(Path(self.pair.local_path).expanduser().absolute()):
            return self._run_locked()

    def _run_locked(self) -> SyncReport:
        qa.check_startup()
        qa.validate_pair(self.pair)
        self.client = qa.restrict_client(self.client)
        report = SyncReport(blocked_before_transfers=True)
        if self.pair.direction not in {"two_way", "up_only", "down_only"}:
            self._report_failure(
                report, f"unsupported sync direction: {self.pair.direction!r}"
            )
            return report
        if self.pair.delete_policy not in {"off", "trash"}:
            self._report_failure(
                report, f"unsupported delete policy: {self.pair.delete_policy!r}"
            )
            return report

        try:
            requested_root = Path(self.pair.local_path).expanduser()
            if self.pair.direction == "down_only" and not requested_root.exists() and not self.db.get_meta(self._binding_key):
                if qa.current_policy():
                    qa.mkdir(requested_root)
                else:
                    requested_root.mkdir(mode=0o700, parents=True)
            self.local_root = canonical_sync_root(requested_root)
            self._suppressed.clear()
            self._check_root()
            local = self.scan_local()
        except SyncCancelled:
            raise
        except Exception as exc:
            self._report_failure(report, f"local root scan failed: {exc}")
            return report

        report.scanned_local_files = len(local)
        report.scanned_local_folders = len(self._local_dirs)
        report.excluded_local = self._local_excluded
        if self._local_scan_errors:
            for error in self._local_scan_errors:
                self._report_failure(report, f"local traversal incomplete: {error}")
            return report

        try:
            self._bind_roots()
        except SyncCancelled:
            raise
        except Exception as exc:
            self._report_failure(report, f"root binding failed: {exc}")
            return report
        last = self.load_last()
        self._canonical_ids = {
            rel: entry.remote_id for rel, entry in last.items() if entry.remote_id
        }
        self._canonical_ids.update(self.pair.canonical_remote_ids)
        self._canonical_paths = {
            entry.remote_id: rel for rel, entry in last.items() if entry.remote_id
        }
        try:
            remote = self._remote_for_run()
        except SyncCancelled:
            raise
        except Exception as exc:
            report.remote_path_conflicts = self._path_conflicts({}, set(local))
            self._report_failure(report, f"remote tree scan failed: {exc}")
            return report
        report.scanned_remote_files = len(remote)
        report.scanned_remote_folders = len(self._remote_dirs)
        report.excluded_remote = self._remote_excluded

        if self.pair.direction != "up_only":
            for rel in sorted(self._duplicate_file_paths):
                candidates = self._path_conflict_candidates.get(rel, {})
                preferred = self._canonical_ids.get(rel)
                if (
                    preferred not in candidates
                    or remote.get(rel) is None
                    or remote[rel].file_id != preferred
                ):
                    self._remote_scan_errors.append("duplicate remote file path")

        if self.pair.direction == "up_only":
            for rel, candidate_ids in sorted(self._duplicate_remote_ids.items()):
                if rel not in local:
                    # Upload-only never touches this remote-only path.
                    continue
                preferred = self._canonical_ids.get(rel)
                if (not preferred or preferred not in candidate_ids
                        or remote.get(rel) is None
                        or remote[rel].file_id != preferred):
                    # No arbitrary choice between distinct Drive IDs. Even
                    # identical bytes would not establish canonical identity.
                    self._remote_scan_errors.append(
                        "ambiguous upload target: duplicate remote file path"
                    )
        # A reviewed ID is an immutable binding, not a preference.  Even if
        # the other duplicate is now the only file at this path, it must not
        # inherit authorization to be updated or downloaded.
        for rel, reviewed_id in sorted(self.pair.canonical_remote_ids.items()):
            current = remote.get(rel)
            if current is None or current.file_id != reviewed_id:
                self._remote_scan_errors.append(
                    "previously reviewed Drive file ID is missing or changed"
                )
                # Expose the remaining object(s) for explicit new review.
                if rel not in self._path_conflict_candidates:
                    self._path_conflict_candidates[rel] = {}
                if current is not None:
                    candidate = self._candidate_from_entry(current)
                    self._path_conflict_candidates[rel][current.file_id] = candidate
                self._path_conflict_reasons[rel] = (
                    "The previously selected Drive file is no longer at this path."
                )
            elif self.pair.direction == "up_only" and current.is_google_doc:
                self._remote_scan_errors.append(
                    "reviewed Google Workspace file cannot be a binary upload target"
                )
                self._path_conflict_candidates[rel] = {
                    current.file_id: self._candidate_from_entry(current)
                }
                self._path_conflict_reasons[rel] = (
                    "A native Google Workspace file cannot accept binary uploads."
                )
        report.remote_path_conflicts = self._path_conflicts(remote, set(local))
        if self._remote_scan_errors:
            self._remote_snapshot = None
            # Keep the validation counter, but do not log an indistinguishable
            # line for every affected path. Names/IDs stay in structured UI
            # data, rather than being disclosed in diagnostic logs.
            for error, count in Counter(self._remote_scan_errors).items():
                detail = f" ({count} checks)" if count > 1 else ""
                self._report_failure(
                    report, f"remote traversal incomplete: {error}{detail}",
                    count=count,
                )
            return report

        local = {rel: entry for rel, entry in local.items() if not self._excluded(rel)}
        remote = {rel: entry for rel, entry in remote.items() if not self._excluded(rel)}
        last = {rel: entry for rel, entry in last.items() if not self._excluded(rel)}
        # Remote-only exclusions (for example shortcuts) can suppress a local
        # directory and all descendants discovered before the remote scan.
        # Use the same included inventory for convergence and completeness.
        self._local_dirs = {rel for rel in self._local_dirs if not self._excluded(rel)}
        self._remote_dirs = {rel for rel in self._remote_dirs if not self._excluded(rel)}
        report.blocked_before_transfers = False
        self._sync_directories(report)
        actions = reconcile(local, remote, last,
                            direction=self.pair.direction,
                            delete_policy=self.pair.delete_policy)
        self._log(f"Sync {self.local_root} <-> {self.pair.remote_folder}: "
                  f"{len(actions)} action(s)")
        for a in actions:
            try:
                self._check_cancel()
                self._check_root()
                self._apply(a, local, remote, report)
            except SyncCancelled:
                raise
            except Exception as exc:
                report.failed += 1
                report.errors.append(f"{a.kind.value} {a.rel}: {exc}")
                LOGGER.exception("Sync action failed: %s %s", a.kind.value, a.rel)

        self._check_cancel()
        self._check_completeness(local, remote, report)
        if report.unexplained_omissions == 0:
            try:
                synchronized_dirs = self._synchronized_directory_map()
                self.db.replace_sync_folders(self.pair_id, synchronized_dirs)
            except Exception as exc:
                self._report_failure(report, f"persist folder state: {exc}")
        # Action execution updates this mapping in place.  It is safe to reuse
        # only when the Changes API subsequently proves nothing remote changed.
        self._remote_snapshot = (
            dict(remote) if not self._path_conflict_candidates else None
        )
        return report

    @staticmethod
    def _report_failure(report: SyncReport, message: str, *, count: int = 1) -> None:
        report.failed += count
        report.errors.append(message)
        LOGGER.error("Sync failed: %s", message)

    def _sync_directories(self, report: SyncReport) -> None:
        """Converge every included directory before processing its files.

        Directory deletion is intentionally conservative: a directory present on
        either authoritative side is recreated on the other side. This preserves
        empty trees and never recursively deletes user content.
        """
        if self.pair.direction in {"up_only", "two_way"}:
            for rel in sorted(self._local_dirs, key=lambda p: (p.count("/"), p)):
                if rel in self._remote_dirs:
                    report.folders_reused += 1
                    continue
                try:
                    self._ensure_remote_dir(rel)
                    self._remote_dirs.add(rel)
                    report.folders_created_remote += 1
                except SyncCancelled:
                    raise
                except Exception as exc:
                    self._report_failure(report, f"create remote folder {rel}: {exc}")

        if self.pair.direction in {"down_only", "two_way"}:
            for rel in sorted(self._remote_dirs, key=lambda p: (p.count("/"), p)):
                try:
                    self._check_cancel()
                    self._check_root()
                    local_dir = safe_local_path(self.local_root, rel)
                    if local_dir.exists():
                        if not local_dir.is_dir():
                            raise ValueError("local path is not a directory")
                        if rel not in self._local_dirs:
                            report.folders_reused += 1
                    else:
                        qa.mkdir(local_dir)
                        report.folders_created_local += 1
                    self._local_dirs.add(rel)
                except SyncCancelled:
                    raise
                except Exception as exc:
                    self._report_failure(report, f"create local folder {rel}: {exc}")

    def _synchronized_directory_map(self) -> dict[str, str]:
        if self.pair.direction == "up_only":
            paths = self._local_dirs
        elif self.pair.direction == "down_only":
            paths = self._remote_dirs
        else:
            paths = self._local_dirs | self._remote_dirs
        # Exclusion suspends a relationship; it does not erase its history or
        # authorize an excluded object to be replaced by a newly created one.
        folders = {
            item.local_rel: item.remote_id
            for item in self.db.list_sync_folders(self.pair_id)
            if self._excluded(item.local_rel)
        }
        folders.update({
            rel: self._dir_ids[rel]
            for rel in paths
            if rel in self._local_dirs and rel in self._remote_dirs and rel in self._dir_ids
        })
        return folders

    def _check_completeness(
        self,
        local: dict[str, LocalEntry],
        remote: dict[str, RemoteEntry],
        report: SyncReport,
    ) -> None:
        """Fail explicitly if an included selected-root entry did not converge."""
        omissions: list[str] = []
        if self.pair.direction == "up_only":
            missing_files = set(local) - set(remote)
            missing_dirs = self._local_dirs - self._remote_dirs
            comparisons = set(local) & set(remote)
        elif self.pair.direction == "down_only":
            missing_files = set(remote) - set(local)
            missing_dirs = self._remote_dirs - self._local_dirs
            comparisons = set(local) & set(remote)
        else:
            missing_files = set(local) ^ set(remote)
            missing_dirs = self._local_dirs ^ self._remote_dirs
            comparisons = set(local) & set(remote)

        omissions.extend(f"file:{rel}" for rel in sorted(missing_files))
        omissions.extend(f"folder:{rel}" for rel in sorted(missing_dirs))
        for rel in sorted(comparisons):
            remote_entry = remote[rel]
            if not remote_entry.is_google_doc and local[rel].md5 != remote_entry.md5:
                omissions.append(f"content:{rel}")

        if omissions:
            report.unexplained_omissions = len(omissions)
            self._report_failure(
                report,
                "completeness assertion failed: " + ", ".join(omissions),
            )

    def _ensure_remote_dir(self, rel_dir: str) -> str:
        if rel_dir in self._dir_ids:
            return self._dir_ids[rel_dir]
        parent = ""
        parent_id = self._dir_ids[""]
        for part in rel_dir.split("/"):
            cur = f"{parent}/{part}" if parent else part
            if cur not in self._dir_ids:
                self._check_cancel()
                self._check_root()
                self._invalidate_remote_snapshot()
                finder = getattr(self.client, "find_folder", None)
                fid = (finder(part, parent_id) if finder else None) or \
                    self.client.create_folder(part, parent_id)
                self._dir_ids[cur] = fid
                self._remote_dirs.add(cur)
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
        qa.validate_pair(self.pair)
        if qa.current_policy() and a.kind in {ActionKind.TRASH_LOCAL, ActionKind.TRASH_REMOTE}:
            raise qa.QaSafetyError("QA safety stop: deletion is forbidden")
        rel = a.rel
        if a.kind == ActionKind.UPLOAD:
            L = local[rel]
            source = safe_local_path(self.local_root, rel)
            existing = remote.get(rel)
            if existing is not None:
                if existing.is_google_doc:
                    if self.pair.direction != "up_only":
                        raise RuntimeError(
                            "refusing to replace a native Google Workspace file with binary media"
                        )
                    # Upload-only keeps the local binary authoritative without
                    # ever media-updating the native file. Drive permits the
                    # binary literal name alongside the extensionless native
                    # object; the scanner gives the export a stable suffix.
                    alternate = self._collision_rel(existing, remote)
                    remote.pop(rel)
                    remote[alternate] = replace(existing, rel=alternate)
                    existing = None

            if existing is not None:
                # This may be a locally disambiguated collision path.  Update
                # media only so the original Drive name and parent stay intact.
                existing = self._confirm_upload_target(rel, existing)
                self._upload(rel, "", existing)
                fid = existing.file_id
            else:
                parent_dir = str(Path(rel).parent.as_posix())
                parent_dir = "" if parent_dir == "." else parent_dir
                parent_id = self._ensure_remote_dir(parent_dir)
                finder = getattr(self.client, "find_file", None)
                found_id = finder(Path(rel).name, parent_id) if callable(finder) else None
                confirmed = self._confirm_upload_target(
                    rel, RemoteEntry(rel, found_id, "", "", 0) if found_id else None)
                if confirmed is not None:
                    self._upload(rel, parent_id, confirmed)
                    fid = confirmed.file_id
                else:
                    fid = self._upload(rel, parent_id, None)
            meta = self.client.get_metadata(fid)
            remote[rel] = RemoteEntry(rel, fid, meta.modified_time or "",
                                      meta.md5_checksum or L.md5, meta.size or L.size,
                                      mime_type=meta.mime_type,
                                      web_view_link=meta.web_view_link)
            self._record(rel, local, remote)
            report.uploaded += 1

        elif a.kind == ActionKind.DOWNLOAD:
            R = remote[rel]
            dest = safe_local_path(self.local_root, rel)
            self._download_remote(R, dest)
            st = dest.stat()
            local[rel] = LocalEntry(rel, st.st_mtime_ns, st.st_size, md5_file(dest))
            self._record(rel, local, remote)
            report.downloaded += 1

        elif a.kind == ActionKind.TRASH_REMOTE:
            self._invalidate_remote_snapshot()
            self.client.trash(remote[rel].file_id)
            remote.pop(rel, None)
            self.db.delete_sync_item(self.pair_id, rel)
            report.trashed_remote += 1

        elif a.kind == ActionKind.TRASH_LOCAL:
            src = safe_local_path(self.local_root, rel)
            if src.exists():
                dest = self._reserve_backup(f"{LOCAL_TRASH}/{rel}")
                with anchored_parent(self.local_root, rel) as (source_fd, name):
                    with anchored_parent(self.local_root, dest.relative_to(self.local_root).as_posix()) as (dest_fd, dest_name):
                        os.replace(name, dest_name, src_dir_fd=source_fd, dst_dir_fd=dest_fd)
            local.pop(rel, None)
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
            # With deletion propagation disabled, preserve the reconciler's
            # historical FORGET decision while restoring the authoritative
            # surviving side in this same complete-tree run.
            if rel in local and rel not in remote and self.pair.direction != "down_only":
                self._apply(Action(ActionKind.UPLOAD, rel), local, remote, report)
            elif rel in remote and rel not in local and self.pair.direction != "up_only":
                self._apply(Action(ActionKind.DOWNLOAD, rel), local, remote, report)

    def _apply_conflict(self, a: Action, local: dict, remote: dict, report: SyncReport):
        qa.validate_pair(self.pair)
        if qa.current_policy() and a.kind in {ActionKind.TRASH_LOCAL, ActionKind.TRASH_REMOTE}:
            raise qa.QaSafetyError("QA safety stop: deletion is forbidden")
        rel = a.rel
        L, R = local[rel], remote[rel]
        canonical = safe_local_path(self.local_root, rel)
        conflict_rel = self._unique_conflict_rel(rel, local, remote)
        conflict_path = safe_local_path(self.local_root, conflict_rel)
        if a.winner == "local":
            # Remote becomes a local conflict copy; local stays canonical and
            # updates the existing remote ID.
            self._download_remote(R, conflict_path)
            self._upload(rel, "", R)
            meta = self.client.get_metadata(R.file_id)
            remote[rel] = RemoteEntry(rel, R.file_id, meta.modified_time or "",
                                      meta.md5_checksum or L.md5, meta.size or L.size,
                                      mime_type=meta.mime_type,
                                      web_view_link=meta.web_view_link)
            st = conflict_path.stat()
            local[conflict_rel] = LocalEntry(
                conflict_rel,
                st.st_mtime_ns,
                st.st_size,
                md5_file(conflict_path),
            )
            self._apply(
                Action(ActionKind.UPLOAD, conflict_rel), local, remote, report
            )
        else:
            # Local becomes a conflict copy; remote is downloaded/exported as
            # canonical.  This is always used for native Workspace documents.
            conflict_path = self._reserve_backup(conflict_rel)
            conflict_rel = conflict_path.relative_to(self.local_root).as_posix()
            self._copy_backup(rel, conflict_path)
            conflict_stat = conflict_path.stat()
            local[conflict_rel] = LocalEntry(
                conflict_rel,
                conflict_stat.st_mtime_ns,
                conflict_stat.st_size,
                md5_file(conflict_path),
            )
            if self.pair.direction != "down_only":
                self._apply(
                    Action(ActionKind.UPLOAD, conflict_rel), local, remote, report
                )
            self._download_remote(R, canonical)
            st = canonical.stat()
            local[rel] = LocalEntry(
                rel, st.st_mtime_ns, st.st_size, md5_file(canonical)
            )
        self._record(rel, local, remote)

    def _unique_conflict_rel(
        self,
        rel: str,
        local: dict[str, LocalEntry],
        remote: dict[str, RemoteEntry],
    ) -> str:
        candidate = _conflict_name(rel)
        if candidate not in local and candidate not in remote:
            return candidate
        path = Path(candidate)
        index = 2
        while True:
            alternate = str(
                path.with_name(f"{path.stem} {index}{path.suffix}")
            )
            if alternate not in local and alternate not in remote:
                return alternate
            index += 1

    def _download_remote(self, remote: RemoteEntry, dest: Path) -> Path:
        if qa.current_policy():
            self._check_cancel()
            if remote.is_google_doc:
                return self.client.export_file(remote.file_id, dest, remote.export_mime, progress_cb=self._check_cancel)
            return self.client.download_file(remote.file_id, dest, progress_cb=self._check_cancel)
        return self._download(remote, dest.relative_to(self.local_root).as_posix())

    def _reserve_backup(self, rel: str) -> Path:
        original = checked_path(self.local_root, rel)
        for attempt in range(100):
            self._check_cancel()
            candidate = original if attempt == 0 else original.with_name(
                f"{original.stem} ({uuid.uuid4().hex}){original.suffix}")
            candidate_rel = candidate.relative_to(self.local_root).as_posix()
            with anchored_parent(self.local_root, candidate_rel, create=True) as (parent_fd, name):
                try:
                    fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                                 0o600, dir_fd=parent_fd)
                except FileExistsError:
                    continue
                os.close(fd)
                return candidate
        raise FileExistsError("Could not reserve a unique backup name")

    def _copy_backup(self, rel: str, dest: Path):
        if qa.current_policy():
            qa.copy_file(self.local_root / rel, dest)
            return
        with opened_source(self.local_root, rel) as source:
            dest_rel = dest.relative_to(self.local_root).as_posix()
            with anchored_parent(self.local_root, dest_rel) as (parent_fd, name):
                fd = os.open(name, os.O_WRONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
                with os.fdopen(fd, 'wb') as output, source.open('rb') as input_file:
                    shutil.copyfileobj(input_file, output)
                    output.flush()
                    os.fsync(output.fileno())

    def _confirm_upload_target(self, rel: str, candidate: RemoteEntry | None) -> RemoteEntry | None:
        """A name/path match at the upload target is not ownership.

        A missing database mapping does not prove a Drive file belongs to
        this sync job. Only a remote_id this pair has itself previously
        recorded for ``rel`` -- via the sync-items history, or an explicit,
        owner-reviewed entry in ``pair.canonical_remote_ids`` -- may be
        trusted as something safe to overwrite. An existing Drive object
        with no such confirmed mapping is never silently adopted, regardless
        of whether any other pair happens to track it either; the caller
        must review its identity and record canonical_remote_ids[rel]
        (the same mechanism the duplicate-review dialog already writes)
        before it can be used as an update target.
        """
        if candidate is None:
            return None
        owned_id = self._canonical_ids.get(rel)
        if owned_id is None:
            raise RuntimeError(
                f"an existing Drive file at {rel!r} is not confirmed as belonging to "
                "this sync pair; review its identity and record it in "
                "canonical_remote_ids before retrying (rename/move it, or resolve the "
                "conflict manually)"
            )
        if candidate.file_id == owned_id:
            return candidate
        verify = getattr(self.client, "get_metadata", None)
        alive = False
        if callable(verify):
            try:
                alive = not verify(owned_id).trashed
            except Exception:
                alive = False
        if not alive:
            raise RuntimeError(
                f"stale Drive ID for {rel}: the previously tracked file {owned_id!r} is "
                "gone and a different, unrecognized file now uses this name; refusing "
                "to overwrite it without confirmation"
            )
        # Keep targeting our own tracked file; ignore the unrelated name match.
        return RemoteEntry(rel, owned_id, "", "", 0)

    def _upload(self, rel: str, parent_id: str, existing: RemoteEntry | None) -> str:
        self._check_cancel()
        self._invalidate_remote_snapshot()
        with opened_source(self.local_root, rel) as path:
            if existing:
                updater = getattr(self.client, 'update_file', None)
                if updater is None:
                    raise ValueError("Drive adapter cannot safely update existing files by ID")
                return updater(existing.file_id, path, progress_cb=self._check_cancel)
            return self.client.upload_file(path, parent_id, Path(rel).name,
                                           progress_cb=self._check_cancel)

    def _download(self, remote: RemoteEntry, rel: str) -> Path:
        self._check_cancel()
        dest = checked_path(self.local_root, rel)
        with anchored_parent(self.local_root, rel, create=True) as (parent_fd, name):
            directory = Path(f'/proc/self/fd/{parent_fd}')
            fd, temporary_name = tempfile.mkstemp(prefix='.electridrive-', suffix='.part', dir=directory)
            os.close(fd)
            temporary = Path(temporary_name)
            try:
                if remote.is_google_doc:
                    self.client.export_file(remote.file_id, temporary, remote.export_mime, progress_cb=self._check_cancel)
                else:
                    self.client.download_file(remote.file_id, temporary, progress_cb=self._check_cancel)
                self._check_cancel()
                if not remote.is_google_doc and (md5_file(temporary) != remote.md5 or temporary.stat().st_size != remote.size):
                    raise ValueError(f"Downloaded content failed integrity check: {rel}")
                self._check_root()
                checked_path(self.local_root, rel)
                self._publish_download(temporary.name, parent_fd, name, rel)
            finally:
                temporary.unlink(missing_ok=True)
        return dest

    def _publish_download(self, temporary_name: str, parent_fd: int, name: str, rel: str) -> None:
        """Preserve the live inode before publishing, including concurrent edits.

        A stat comparison alone has a race between checking and replacing. Move
        any live destination into a unique recovery slot instead, then use an
        atomic no-clobber link so a newly created destination also survives.
        Writers already holding the old inode continue writing the recovery copy.
        """
        recovery_rel = None
        try:
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            recovery = self._reserve_backup(f"{LOCAL_TRASH}/overwritten/{rel}")
            candidate_rel = recovery.relative_to(self.local_root).as_posix()
            with anchored_parent(self.local_root, candidate_rel) as (recovery_fd, recovery_name):
                try:
                    os.replace(name, recovery_name, src_dir_fd=parent_fd, dst_dir_fd=recovery_fd)
                except FileNotFoundError:
                    # The destination disappeared meanwhile; remove only our
                    # unused reservation and still publish without clobbering.
                    os.unlink(recovery_name, dir_fd=recovery_fd)
                else:
                    recovery_rel = candidate_rel
        try:
            self._check_cancel()
            os.link(temporary_name, name, src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd, follow_symlinks=False)
        except BaseException:
            if recovery_rel is not None:
                # Restore the old inode when publication was cancelled/failed,
                # but never overwrite a file another process created meanwhile.
                with anchored_parent(self.local_root, recovery_rel) as (recovery_fd, recovery_name):
                    try:
                        os.link(recovery_name, name, src_dir_fd=recovery_fd,
                                dst_dir_fd=parent_fd, follow_symlinks=False)
                    except FileExistsError:
                        pass
                    except OSError:
                        LOGGER.exception('Could not restore destination; preserved at %s', recovery_rel)
            raise
