#!/usr/bin/env python3
"""PR #19 live safety acceptance against the owner's EXISTING synthetic QA folder.

Runs the real sync engine against real Drive listing/metadata, but replaces
every engine mutation operation with a rejecting guard.  Temporarily renames
ONLY the previously verified synthetic candidate A (or B) and restores it in
finally.  No personal sync settings, Drive folders or user files are scanned.
The optional 'restore' command recovers from an interrupted previous run.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from uuid import uuid4

QA = Path.home() / "Downloads/ElectriDrive-DUP-LIVE-1f79ea3"
SOURCE = QA / "source"
MANIFEST = QA / "qa-manifest.json"
ORIGINAL_PROFILE = QA / "profile"
RECOVERY = QA / "pr19-stale-recovery.json"
EXPECTED_BRANCH = "fix/stale-canonical-drive-id"
ORIGINAL_NAME = "duplicate.txt"


def stop(reason: str) -> None:
    raise SystemExit("STOP (PR19-QA): " + reason)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def children(g, folder_id: str):
    result = []
    page = None
    seen = set()
    while True:
        listing = g.list_folder(folder_id, page)
        result.extend(listing.files)
        page = listing.next_page_token
        if not page:
            return result
        if page in seen:
            stop("Drive repeated a page token.")
        seen.add(page)


def load_qa():
    for path in (QA, SOURCE, ORIGINAL_PROFILE, MANIFEST):
        if not path.exists() or path.is_symlink():
            stop("Original QA fixture missing or unsafe: " + str(path))
    if MANIFEST.is_symlink():
        stop("QA manifest is a symlink.")
    state = json.loads(MANIFEST.read_text(encoding="utf-8"))
    ids = state.get("candidate_ids", [])
    if (state.get("phase") != "preflight_blocked_without_writes"
            or len(ids) != 2 or len(set(ids)) != 2
            or not state.get("remote_folder_id")
            or not str(state.get("remote_folder", "")).startswith("ElectriDrive-QA-DUP-")
            or state.get("source") != str(SOURCE)):
        stop("QA manifest is not the expected synthetic duplicate fixture.")
    expected = state.get("local_sha256", {})
    if not (SOURCE / ORIGINAL_NAME).is_file() or not (SOURCE / ".env").is_file():
        stop("QA source files are missing.")
    if any(sha256((SOURCE / name).read_bytes()) != expected.get(name)
           for name in (ORIGINAL_NAME, ".env")):
        stop("QA source bytes have changed.")
    return state


def validate_settings(cfg: Path, state: dict):
    if not cfg.is_file() or cfg.is_symlink():
        stop("Isolated settings missing or unsafe.")
    from electridrive.config import Settings
    settings = Settings.from_dict(json.loads(cfg.read_text(encoding="utf-8")))
    if settings.scope != "file" or len(settings.sync_pairs) != 1:
        stop("QA profile contains extra pairs or wrong Drive scope.")
    pair = settings.sync_pairs[0]
    if (pair.local_path != str(SOURCE)
            or pair.remote_folder != state["remote_folder"]
            or pair.direction != "up_only"
            or pair.enabled
            or pair.delete_policy != "off"
            or not pair.include_all_files
            or pair.canonical_remote_ids.keys() != {ORIGINAL_NAME}):
        stop("QA pair is not exactly the isolated disabled upload-only pair.")
    chosen = pair.canonical_remote_ids[ORIGINAL_NAME]
    if chosen not in state["candidate_ids"]:
        stop("Saved reviewed Drive ID is not a synthetic candidate.")
    return pair, chosen


def validate_remote(g, state: dict, chosen: str, *, temporary: str | None):
    folder = g.get_metadata(state["remote_folder_id"])
    if (not folder.is_folder or folder.name != state["remote_folder"]
            or folder.trashed or "root" not in folder.parents):
        stop("Synthetic QA folder ID is no longer rooted at My Drive.")
    all_files = children(g, folder.id)
    ids = state["candidate_ids"]
    if len(all_files) != 3 or {f.id for f in all_files} != set(ids) | {
        next((f.id for f in all_files if f.name == ".env"), "")
    }:
        stop("QA cloud folder has unexpected members.")
    by_id = {f.id: f for f in all_files}
    other = next(x for x in ids if x != chosen)
    chosen_file = by_id[chosen]
    if (chosen_file.name != (temporary or ORIGINAL_NAME)
            or chosen_file.parents != (folder.id,)
            or chosen_file.md5_checksum != md5((SOURCE / ORIGINAL_NAME).read_bytes())):
        stop("Selected synthetic QA file is not in its expected state.")
    untouched = by_id[other]
    original_bytes = (QA / ("seed-A.txt" if other == ids[0] else "seed-B.txt")).read_bytes()
    if (untouched.name != ORIGINAL_NAME
            or untouched.parents != (folder.id,)
            or untouched.md5_checksum != md5(original_bytes)):
        stop("Untouched QA duplicate has changed.")
    env_files = [f for f in all_files if f.name == ".env"]
    if (len(env_files) != 1
            or env_files[0].md5_checksum != md5((SOURCE / ".env").read_bytes())):
        stop("QA .env file has changed.")
    return all_files


def write_recovery(info: dict):
    fd = os.open(str(RECOVERY), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump(info, out, indent=2)
        out.flush()
        os.fsync(out.fileno())


def rename_exact(g, selected_id: str, new_name: str):
    return g.service.files().update(
        fileId=selected_id, body={"name": new_name},
        fields="id,name,parents"
    ).execute()


class NoWriteClient:
    """Narrowest real-Drive reader surface for the actual sync engine.

    Fail closed on ANY unrecognized access, including service and media writes.
    """
    get_start_page_token = None
    list_changes = None

    def __init__(self, real, root_id: str, remote_folder: str):
        self.real = real
        self.root_id = root_id
        self.remote_folder = remote_folder
        self.unsafe_calls = []

    def ensure_folder_path(self, path):
        if path != self.remote_folder:
            self.unsafe_calls.append("unexpected-root")
            raise RuntimeError("Refused unexpected remote folder")
        return self.root_id

    def list_folder(self, parent_id="root", page_token=None):
        if parent_id != self.root_id:
            self.unsafe_calls.append("unexpected-list")
            raise RuntimeError("Refused listing outside QA folder")
        return self.real.list_folder(parent_id, page_token)

    def find_file(self, name, parent_id):
        if parent_id != self.root_id:
            self.unsafe_calls.append("unexpected-find")
            raise RuntimeError("Refused search outside QA folder")
        return self.real.find_file(name, parent_id)

    def get_metadata(self, file_id):
        self.unsafe_calls.append("unexpected-metadata")
        raise RuntimeError("Metadata read not required by fail-closed scan")

    def __getattr__(self, name):
        self.unsafe_calls.append(name)
        raise RuntimeError("QA guard refused sync client operation: " + name)


def prepare_credentials(isolated_profile: Path):
    src = ORIGINAL_PROFILE / "config" / "electridrive"
    dest = isolated_profile / "config" / "electridrive"
    if not src.is_dir() or src.is_symlink():
        stop("Original QA config directory is missing or symlinked.")
    dest.mkdir(parents=True, mode=0o700)
    # Copy only the existing isolated QA credentials and settings, never HOME config.
    for name in ("settings.json", "app_client.json", "credentials.json", "token.json"):
        file = src / name
        if file.exists():
            if not file.is_file() or file.is_symlink():
                stop("Unsafe QA credential source.")
            shutil.copyfile(file, dest / name)
            (dest / name).chmod(0o600)
    return dest / "settings.json"


def configure_profile(profile: Path):
    os.environ["XDG_CONFIG_HOME"] = str(profile / "config")
    os.environ["XDG_STATE_HOME"] = str(profile / "state")
    os.environ["XDG_CACHE_HOME"] = str(profile / "cache")
    os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.fail.Keyring"
    os.environ["PYTHONNOUSERSITE"] = "1"
    os.environ.pop("ELECTRIDRIVE_SCOPE", None)


def run():
    if RECOVERY.exists():
        stop("Unfinished QA rename exists. Use this script with 'restore' first.")
    root = Path(__file__).resolve().parents[1]
    branch = subprocess.check_output(
        ["git", "-C", str(root), "branch", "--show-current"], text=True
    ).strip()
    if branch != EXPECTED_BRANCH:
        stop("Run only from checkout branch " + EXPECTED_BRANCH)
    state = load_qa()
    with TemporaryDirectory(prefix="pr19-isolated-", dir=QA) as tmp:
        profile = Path(tmp) / "profile"
        cfg = prepare_credentials(profile)
        configure_profile(profile)
        pair, selected_id = validate_settings(cfg, state)
        from electridrive.google_api.client import GoogleDriveClient
        from electridrive.storage.database import SyncDatabase
        from electridrive.config import get_paths
        from electridrive.sync.twoway import TwoWaySyncEngine

        g = GoogleDriveClient()
        validate_remote(g, state, selected_id, temporary=None)
        temporary = ".PR19-QA-RENAMED-" + uuid4().hex + ".txt"
        write_recovery({
            "remote_folder_id": state["remote_folder_id"],
            "remote_folder": state["remote_folder"],
            "selected_id": selected_id,
            "temporary_name": temporary,
            "original_name": ORIGINAL_NAME,
        })
        test_ok = False
        try:
            rename_exact(g, selected_id, temporary)
            validate_remote(g, state, selected_id, temporary=temporary)
            guarded = NoWriteClient(g, state["remote_folder_id"], state["remote_folder"])
            db = SyncDatabase(get_paths().database_file)
            try:
                report = TwoWaySyncEngine(guarded, db, pair).run()
            finally:
                db.close()
            if (report.failed < 1
                    or report.uploaded != 0 or report.downloaded != 0
                    or report.trashed_remote != 0 or report.trashed_local != 0
                    or guarded.unsafe_calls
                    or not any("previously reviewed Drive file ID" in item
                               for item in report.errors)
                    or not any(c.remote_path == ORIGINAL_NAME
                               and not c.resolved
                               for c in report.remote_path_conflicts)):
                stop("FAIL: sync was not safely blocked before any write attempt.")
            validate_remote(g, state, selected_id, temporary=temporary)
            test_ok = True
        finally:
            # Only the previously checked synthetic selected ID is restored.
            current = g.get_metadata(selected_id)
            if (current.name == temporary
                    and current.parents == (state["remote_folder_id"],)
                    and not current.trashed):
                rename_exact(g, selected_id, ORIGINAL_NAME)
            elif current.name != ORIGINAL_NAME:
                stop("Manual recovery required: selected QA file no longer has expected name.")
            validate_remote(g, state, selected_id, temporary=None)
            RECOVERY.unlink()
        if test_ok:
            print("PR19 LIVE-SAFETY PASS: stale reviewed ID blocked.")
            print("Engine mutation attempts: 0; uploads/downloads/trash: 0.")
            print("Selected synthetic QA file restored by ID.")
            print("Other synthetic duplicate and .env unchanged.")
            print("Personal sync pairs/configurations not accessed.")


def restore():
    if not RECOVERY.is_file() or RECOVERY.is_symlink():
        stop("No valid recovery record.")
    record = json.loads(RECOVERY.read_text(encoding="utf-8"))
    state = load_qa()
    if (record.get("remote_folder_id") != state["remote_folder_id"]
            or record.get("remote_folder") != state["remote_folder"]
            or record.get("selected_id") not in state["candidate_ids"]
            or record.get("original_name") != ORIGINAL_NAME
            or not str(record.get("temporary_name", "")).startswith(".PR19-QA-RENAMED-")):
        stop("Recovery record does not belong to this isolated QA fixture.")
    configure_profile(ORIGINAL_PROFILE)
    from electridrive.google_api.client import GoogleDriveClient
    g = GoogleDriveClient()
    fid = record["selected_id"]
    current = g.get_metadata(fid)
    if current.parents != (state["remote_folder_id"],) or current.trashed:
        stop("QA selected file was moved/trashed externally; no automatic recovery.")
    if current.name == record["temporary_name"]:
        rename_exact(g, fid, ORIGINAL_NAME)
    elif current.name != ORIGINAL_NAME:
        stop("QA file has unexpected name; no automatic changes.")
    validate_remote(g, state, fid, temporary=None)
    RECOVERY.unlink()
    print("PR19 QA RESTORED: synthetic selected Drive file returned to original name.")


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("run", "restore"):
        stop("Usage: python scripts/qa_stale_canonical_live.py run|restore")
    os.umask(0o077)
    {"run": run, "restore": restore}[sys.argv[1]]()
