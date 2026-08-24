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

import hashlib
import logging
import shutil
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from pathlib import Path

from electridrive.google_api.client import DRIVE_SHORTCUT_MIME, export_format_for
from electridrive.sync.downloader import sanitize_name
from electridrive.sync.hasher import md5_file
from electridrive.sync.rules import SyncRules
from electridrive.sync.tree import canonical_sync_root, safe_local_path, scan_local_tree

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
                 log_cb=None):
        self.client = client
        self.db = db
        self.pair = pair
        self.rules = rules or SyncRules()
        self.log_cb = log_cb
        self.local_root = Path(pair.local_path).expanduser()
        self.pair_id = pair_id_for(pair.local_path, pair.remote_folder)
        self.change_token_key = f"two-way:{self.pair_id}"
        self._dir_ids: dict[str, str] = {}
        self._canonical_ids: dict[str, str] = {}
        self._canonical_paths: dict[str, str] = {}
        self._remote_snapshot: dict[str, RemoteEntry] | None = None
        self._local_dirs: set[str] = set()
        self._remote_dirs: set[str] = set()
        self._local_scan_errors: list[str] = []
        self._remote_scan_errors: list[str] = []
        self._local_excluded = 0
        self._remote_excluded = 0

    def _log(self, msg: str):
        LOGGER.info(msg)
        if self.log_cb:
            self.log_cb(msg)

    # ------------------------------------------------------------- snapshots
    def scan_local(self) -> dict[str, LocalEntry]:
        out: dict[str, LocalEntry] = {}
        tree = scan_local_tree(self.local_root, self.rules)
        self.local_root = tree.root
        self._local_dirs = tree.relative_directories
        self._local_scan_errors = list(tree.errors)
        self._local_excluded = len(tree.excluded)
        for path in tree.files:
            rel = path.relative_to(tree.root).as_posix()
            try:
                path = safe_local_path(tree.root, rel)
                if not path.is_file():
                    raise OSError("selected entry is no longer a regular file")
                st = path.stat()
                out[rel] = LocalEntry(
                    rel, st.st_mtime_ns, st.st_size, md5_file(path)
                )
            except OSError as exc:
                self._local_scan_errors.append(
                    f"cannot read {rel}: {type(exc).__name__}"
                )
        return out

    def scan_remote(self) -> dict[str, RemoteEntry]:
        root_id = self.client.ensure_folder_path(self.pair.remote_folder)
        self._dir_ids = {"": root_id}
        self._remote_dirs = set()
        self._remote_scan_errors = []
        self._remote_excluded = 0
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
        while True:
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
                if f.is_folder:
                    if self.rules.is_excluded_relative(rel, is_file=False):
                        self._remote_excluded += 1
                        continue
                    if rel in self._dir_ids or rel in entries:
                        self._remote_scan_errors.append(
                            f"remote path collision at {rel!r}"
                        )
                        continue
                    if f.id in visited:
                        self._remote_scan_errors.append(
                            f"remote folder cycle at {rel!r}"
                        )
                        continue
                    self._dir_ids[rel] = f.id
                    self._remote_dirs.add(rel)
                    visited.add(f.id)
                    self._walk_remote(f.id, rel, entries, visited)
                elif self.rules.is_excluded_relative(
                    rel, is_file=True, size=f.size
                ) or is_shortcut:
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
                        ),
                    )
                else:
                    self._remote_scan_errors.append(
                        f"remote file has no synchronizable content fingerprint: {rel!r}"
                    )
            page = listing.next_page_token
            if not page:
                break

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
            self._remote_scan_errors.append(
                f"remote file/folder path collision at {candidate.rel!r}"
            )
            return
        if existing is None:
            entries[candidate.rel] = candidate
            return

        preferred = self._canonical_ids.get(candidate.rel)
        cross_type_collision = existing.is_google_doc != candidate.is_google_doc
        if not cross_type_collision:
            if preferred and candidate.file_id == preferred:
                entries[candidate.rel] = candidate
            self._remote_scan_errors.append(
                f"duplicate remote file path at {candidate.rel!r}"
            )
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
            # With no history, preserve the actual binary's literal Drive name.
            alternate = self._collision_rel(existing, entries)
            entries.pop(candidate.rel)
            entries[alternate] = replace(existing, rel=alternate)
            entries[candidate.rel] = candidate
        else:
            alternate = self._collision_rel(candidate, entries)
            entries[alternate] = replace(candidate, rel=alternate)

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
            out[it.local_rel] = LastEntry(
                it.local_rel, it.sha256, it.remote_md5, it.remote_id
            )
        return out

    # ---------------------------------------------------------- Changes API
    def _consume_changes(self, page_token: str) -> tuple[bool, str]:
        """Drain a Changes feed and return (had_changes, resume_token)."""
        page = page_token
        had_changes = False
        while True:
            changes, next_page, new_start = self.client.list_changes(page)
            had_changes = had_changes or bool(changes)
            if next_page:
                if next_page == page:
                    raise RuntimeError("Drive Changes API returned a repeated page token")
                page = next_page
                continue
            if not new_start:
                raise RuntimeError("Drive Changes API did not return a resume token")
            return had_changes, new_start

    def _scan_with_new_change_baseline(self) -> dict[str, RemoteEntry]:
        """Establish a token around a correctness-first full tree scan."""
        starter = getattr(self.client, "get_start_page_token", None)
        lister = getattr(self.client, "list_changes", None)
        if not callable(starter) or not callable(lister):
            return self.scan_remote()
        try:
            baseline = starter()
        except Exception as exc:
            LOGGER.debug("Changes API start token unavailable; using full scan: %s", exc)
            return self.scan_remote()

        snapshot = self.scan_remote()
        try:
            changed_during_scan, resume = self._consume_changes(baseline)
        except Exception as exc:
            # The full scan remains authoritative.  Do not advance a token when
            # its change window could not be verified.
            LOGGER.debug("Changes API baseline failed; retaining full scan: %s", exc)
            return snapshot
        if changed_during_scan:
            # A mutation may have landed in a folder already visited.  Repeat the
            # full scan rather than trying a risky partial reconciliation.
            snapshot = self.scan_remote()
        self.db.set_change_token(self.change_token_key, resume)
        return snapshot

    def _remote_for_run(self) -> dict[str, RemoteEntry]:
        """Use Changes only to reuse a known-complete in-memory snapshot.

        Any reported change, missing cache, invalid token, or API failure falls
        back to a complete folder-tree scan.
        """
        token = self.db.get_change_token(self.change_token_key)
        lister = getattr(self.client, "list_changes", None)
        if not token or not callable(lister):
            return self._scan_with_new_change_baseline()

        try:
            changed, resume = self._consume_changes(token)
        except Exception as exc:
            LOGGER.debug("Changes API token failed; rebuilding with full scan: %s", exc)
            return self._scan_with_new_change_baseline()

        if self._remote_snapshot is not None and not changed:
            self.db.set_change_token(self.change_token_key, resume)
            return dict(self._remote_snapshot)

        # A persisted token is useful across process restarts for detection, but
        # sync_items is not a complete remote index.  Without an in-memory tree,
        # or when anything changed, correctness requires a full scan.
        snapshot = self.scan_remote()
        self.db.set_change_token(self.change_token_key, resume)
        return snapshot

    # --------------------------------------------------------------- execute
    def run(self) -> SyncReport:
        report = SyncReport()
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
            if self.pair.direction == "down_only" and not requested_root.exists():
                requested_root.mkdir(mode=0o700, parents=True)
            self.local_root = canonical_sync_root(requested_root)
            local = self.scan_local()
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

        last = self.load_last()
        self._canonical_ids = {
            rel: entry.remote_id for rel, entry in last.items() if entry.remote_id
        }
        self._canonical_paths = {
            entry.remote_id: rel for rel, entry in last.items() if entry.remote_id
        }
        try:
            remote = self._remote_for_run()
        except Exception as exc:
            self._report_failure(report, f"remote tree scan failed: {exc}")
            return report
        report.scanned_remote_files = len(remote)
        report.scanned_remote_folders = len(self._remote_dirs)
        report.excluded_remote = self._remote_excluded
        if self._remote_scan_errors:
            for error in self._remote_scan_errors:
                self._report_failure(report, f"remote traversal incomplete: {error}")
            return report

        self._sync_directories(report)
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

        self._check_completeness(local, remote, report)
        if report.unexplained_omissions == 0:
            synchronized_dirs = self._synchronized_directory_map()
            try:
                self.db.replace_sync_folders(self.pair_id, synchronized_dirs)
            except Exception as exc:
                self._report_failure(report, f"persist folder state: {exc}")
        # Action execution updates this mapping in place.  It is safe to reuse
        # only when the Changes API subsequently proves nothing remote changed.
        self._remote_snapshot = dict(remote)
        return report

    @staticmethod
    def _report_failure(report: SyncReport, message: str) -> None:
        report.failed += 1
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
                except Exception as exc:
                    self._report_failure(report, f"create remote folder {rel}: {exc}")

        if self.pair.direction in {"down_only", "two_way"}:
            for rel in sorted(self._remote_dirs, key=lambda p: (p.count("/"), p)):
                try:
                    local_dir = safe_local_path(self.local_root, rel)
                    if local_dir.exists():
                        if not local_dir.is_dir():
                            raise ValueError("local path is not a directory")
                        if rel not in self._local_dirs:
                            report.folders_reused += 1
                    else:
                        local_dir.mkdir(parents=True)
                        report.folders_created_local += 1
                    self._local_dirs.add(rel)
                except Exception as exc:
                    self._report_failure(report, f"create local folder {rel}: {exc}")

    def _synchronized_directory_map(self) -> dict[str, str]:
        if self.pair.direction == "up_only":
            paths = self._local_dirs
        elif self.pair.direction == "down_only":
            paths = self._remote_dirs
        else:
            paths = self._local_dirs | self._remote_dirs
        return {
            rel: self._dir_ids[rel]
            for rel in paths
            if rel in self._local_dirs and rel in self._remote_dirs and rel in self._dir_ids
        }

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
                self.client.update_file(existing.file_id, source)
                fid = existing.file_id
            else:
                parent_dir = str(Path(rel).parent.as_posix())
                parent_dir = "" if parent_dir == "." else parent_dir
                parent_id = self._ensure_remote_dir(parent_dir)
                finder = getattr(self.client, "find_file", None)
                fid = finder(Path(rel).name, parent_id) if callable(finder) else None
                if fid:
                    self.client.update_file(fid, source)
                else:
                    fid = self.client.upload_file(source, parent_id, Path(rel).name)
            meta = self.client.get_metadata(fid)
            remote[rel] = RemoteEntry(rel, fid, meta.modified_time or "",
                                      meta.md5_checksum or L.md5, meta.size or L.size)
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
            self.client.trash(remote[rel].file_id)
            remote.pop(rel, None)
            self.db.delete_sync_item(self.pair_id, rel)
            report.trashed_remote += 1

        elif a.kind == ActionKind.TRASH_LOCAL:
            src = safe_local_path(self.local_root, rel)
            if src.exists():
                dest = safe_local_path(self.local_root, f"{LOCAL_TRASH}/{rel}")
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dest))
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
        rel = a.rel
        L, R = local[rel], remote[rel]
        canonical = safe_local_path(self.local_root, rel)
        conflict_rel = self._unique_conflict_rel(rel, local, remote)
        conflict_path = safe_local_path(self.local_root, conflict_rel)
        if a.winner == "local":
            # Remote becomes a local conflict copy; local stays canonical and
            # updates the existing remote ID.
            self._download_remote(R, conflict_path)
            self.client.update_file(R.file_id, canonical)
            meta = self.client.get_metadata(R.file_id)
            remote[rel] = RemoteEntry(rel, R.file_id, meta.modified_time or "",
                                      meta.md5_checksum or L.md5, meta.size or L.size)
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
            conflict_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(canonical, conflict_path)
            conflict_stat = conflict_path.stat()
            local[conflict_rel] = LocalEntry(
                conflict_rel,
                conflict_stat.st_mtime_ns,
                conflict_stat.st_size,
                md5_file(conflict_path),
            )
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
        if remote.is_google_doc:
            return self.client.export_file(
                remote.file_id, dest, remote.export_mime
            )
        return self.client.download_file(remote.file_id, dest)
