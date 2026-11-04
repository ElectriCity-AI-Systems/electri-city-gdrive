from __future__ import annotations

import argparse
import logging
import platform
import sys

from electridrive import qa_policy as qa
from pathlib import Path

from electridrive.config import SyncPair, get_paths, load_settings, selected_scopes
from electridrive.google_api.client import GoogleDriveClient
from electridrive.logging_setup import configure_logging
from electridrive.storage.database import SyncDatabase
from electridrive.sync.engine import UploadOnlySyncEngine

LOGGER = logging.getLogger(__name__)


def cmd_doctor(args: argparse.Namespace) -> int:
    from electridrive.vfs import fuse_available
    from electridrive.google_api import app_client

    paths = get_paths()
    print("ElectriDrive doctor")
    print(f"Python runtime:   {platform.python_version()}")
    print(f"Config dir:       {paths.config_dir}")
    print(f"State dir:        {paths.state_dir}")
    print(f"Cache dir:        {paths.cache_dir}")
    print(f"Credentials file: {paths.credentials_file} {'OK' if paths.credentials_file.exists() else 'MISSING'}")
    print(f"Database file:    {paths.database_file}")
    print(f"OAuth scopes:     {', '.join(selected_scopes())}")
    print(f"OAuth client:     {'configured' if app_client.is_configured() else 'unavailable'}")
    print(f"Google Picker:    {'configured' if app_client.is_picker_configured() else 'unavailable'}")
    print(f"FUSE available:   {'yes' if fuse_available() else 'no'}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    client = qa.restrict_client(GoogleDriveClient())
    files = client.list_files(limit=args.limit)
    for item in files:
        print(f"{item.id}\t{item.name}\t{item.mime_type or ''}\t{item.size or ''}")
    return 0


def _parse_confirmed_remote_ids(pairs: list[str] | None) -> dict[str, str]:
    """Parse repeated --confirm-remote-id REL=ID options into a dict."""
    confirmed: dict[str, str] = {}
    for item in pairs or []:
        rel, sep, remote_id = item.partition("=")
        if not sep or not rel or not remote_id:
            raise ValueError(f"--confirm-remote-id must be REL=ID, got {item!r}")
        confirmed[rel] = remote_id
    return confirmed


def cmd_sync_up(args: argparse.Namespace) -> int:
    qa.validate_pair(SyncPair(local_path=str(Path(args.local_folder).expanduser()),
                              remote_folder=args.remote_folder, direction="up_only", delete_policy="off"))
    confirmed_remote_ids = _parse_confirmed_remote_ids(getattr(args, "confirm_remote_id", None))
    paths = get_paths()
    db = SyncDatabase(paths.database_file)
    try:
        engine = UploadOnlySyncEngine(
            drive_client=GoogleDriveClient(),
            database=db,
            log_callback=lambda msg: print(msg),
            confirmed_remote_ids=confirmed_remote_ids,
        )
        result = engine.sync_up(Path(args.local_folder), args.remote_folder)
        return 0 if result.failed == 0 else 2
    finally:
        db.close()


def cmd_sync(args: argparse.Namespace) -> int:
    from electridrive.sync.twoway import TwoWaySyncEngine

    from electridrive.sync_schedule import periodic_sync, validate_interval
    interval = getattr(args, "interval", None)
    if interval is not None:
        validate_interval(interval)
    paths = get_paths()
    db = SyncDatabase(paths.database_file)
    try:
        pair = SyncPair(local_path=str(Path(args.local_folder).expanduser()),
                        remote_folder=args.remote_folder, direction=args.direction,
                        delete_policy="off" if qa.current_policy() else "trash",
                        canonical_remote_ids=_parse_confirmed_remote_ids(
                            getattr(args, "confirm_remote_id", None)))
        qa.validate_pair(pair)
        engine = TwoWaySyncEngine(GoogleDriveClient(), db, pair, log_cb=print)
        def run():
            qa.check_startup()
            qa.validate_pair(pair)
            return engine.run()
        reports = periodic_sync(run, interval) if interval is not None else (run(),)
        failed = False
        for report in reports:
            print(f"Done. up={report.uploaded} down={report.downloaded} "
                  f"trash_remote={report.trashed_remote} trash_local={report.trashed_local} "
                  f"conflicts={report.conflicts} failed={report.failed}")
            for err in report.errors:
                print(f"  ! {err}")
            failed = failed or bool(report.failed or report.unexplained_omissions)
        return 2 if failed else 0
    finally:
        db.close()


def cmd_sync_configured(args: argparse.Namespace) -> int:
    from electridrive.sync.runner import run_configured_pairs

    paths = get_paths()
    db = SyncDatabase(paths.database_file)
    try:
        result = run_configured_pairs(
            GoogleDriveClient(), db, load_settings().sync_pairs, log_cb=print
        )
        print(
            f"Configured sync complete. configured={result.configured_pairs} "
            f"enabled={result.enabled_pairs} successful={result.successful_pairs} "
            f"failed={result.failed_pairs}"
        )
        for error in result.errors:
            print(f"  ! {error}")
        return 0 if result.failed_pairs == 0 else 2
    finally:
        db.close()


def cmd_download(args: argparse.Namespace) -> int:
    qa.require_local(Path(args.dest).expanduser(), exists=False)
    client = qa.restrict_client(GoogleDriveClient())
    remote = client.get_metadata(args.file_id)
    dest = Path(args.dest).expanduser()
    from electridrive.transfers import TransferManager, TransferState
    manager = TransferManager(client)
    try:
        manager.enqueue_download(remote, dest)
        manager.wait_idle()
        failed = 0
        for transfer in manager.snapshot():
            if transfer.state == TransferState.DONE:
                print(f"downloaded {transfer.dest_path}")
            else:
                failed += 1
                print(f"FAILED {transfer.name}: {transfer.error or transfer.state.value}")
        return 0 if failed == 0 else 2
    finally:
        manager.shutdown(wait=True)


def cmd_mount(args: argparse.Namespace) -> int:
    if qa.current_policy():
        print("QA safety: Virtual Drive/FUSE is disabled.")
        return 2
    from electridrive.vfs import FuseMount, fuse_available

    if getattr(args, "writable", False):
        print(
            "Writable mounts are not supported in ElectriDrive 2.1.2; "
            "Virtual Drive is read-only."
        )
        return 2
    if not fuse_available():
        print("FUSE is not available (need libfuse + fusermount).")
        return 1
    paths = get_paths()
    mount = FuseMount(GoogleDriveClient(), paths.vfs_cache_dir)
    mount.start(args.mountpoint, args.remote_folder or "")
    print(f"Mounted Drive read-only at {args.mountpoint}. Press Ctrl+C to unmount.")
    try:
        mount.wait()
    except KeyboardInterrupt:
        print("\nUnmounting…")
        mount.stop()
    return 0


def cmd_unmount(args: argparse.Namespace) -> int:
    import shutil
    import subprocess

    tool = shutil.which("fusermount3") or shutil.which("fusermount")
    if not tool:
        print("fusermount not found.")
        return 1
    result = subprocess.run([tool, "-u", args.mountpoint], check=False,
                            capture_output=True, text=True, timeout=10)
    if result.returncode:
        print(f"Unmount failed: {getattr(result, 'stderr', '') or result.returncode}")
        return 1
    print(f"Unmounted {args.mountpoint}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="electridrive", description="ElectriDrive — Electric-City Drive for Linux")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("qa-init", help="Seal an empty synthetic QA profile")
    p_init.add_argument("--profile", required=True)
    p_init.add_argument("--local-root", required=True)
    p_init.set_defaults(func=cmd_qa_init)
    p_create = sub.add_parser("qa-create-folder", help="Explicitly create a new synthetic cloud root")
    p_create.add_argument("--name", required=True)
    p_create.set_defaults(func=cmd_qa_create_folder)

    sub.add_parser("doctor", help="Show local configuration status").set_defaults(func=cmd_doctor)

    p_list = sub.add_parser("list", help="List Drive files")
    p_list.add_argument("--limit", type=int, default=20)
    p_list.set_defaults(func=cmd_list)

    p_up = sub.add_parser("sync-up", help="Upload-only sync (safe, never deletes)")
    p_up.add_argument("local_folder")
    p_up.add_argument("--remote-folder", required=True)
    p_up.add_argument(
        "--confirm-remote-id", action="append", metavar="REL=ID", default=[],
        help="Explicitly confirm that the existing Drive file ID belongs to this sync "
             "pair at the given local relative path, after reviewing its identity. "
             "Repeatable. Required to adopt any pre-existing, untracked Drive file.",
    )
    p_up.set_defaults(func=cmd_sync_up)

    p_sync = sub.add_parser("sync", help="Two-way sync a local folder with Drive")
    p_sync.add_argument("local_folder")
    p_sync.add_argument("--remote-folder", required=True)
    p_sync.add_argument("--direction", choices=["two_way", "up_only", "down_only"],
                        default="two_way")
    p_sync.add_argument("--interval", type=float,
                        help="Repeat sequentially every N seconds; stop on sync errors")
    p_sync.add_argument(
        "--confirm-remote-id", action="append", metavar="REL=ID", default=[],
        help="Explicitly confirm that the existing Drive file ID belongs to this sync "
             "pair at the given local relative path, after reviewing its identity. "
             "Repeatable. Required to adopt any pre-existing, untracked Drive file.",
    )
    p_sync.set_defaults(func=cmd_sync)

    sub.add_parser(
        "sync-configured", help="Sync every enabled pair saved in settings"
    ).set_defaults(func=cmd_sync_configured)

    p_dl = sub.add_parser("download", help="Download a file/folder by Drive file id")
    p_dl.add_argument("file_id")
    p_dl.add_argument("dest")
    p_dl.set_defaults(func=cmd_download)

    p_mount = sub.add_parser("mount", help="Mount Drive as a virtual filesystem (no rclone)")
    p_mount.add_argument("mountpoint")
    p_mount.add_argument("--remote-folder", default="")
    # Retain a hidden compatibility flag only to reject old writable-mount commands
    # with an explicit safety message before touching FUSE or the mountpoint.
    p_mount.add_argument("--writable", action="store_true", help=argparse.SUPPRESS)
    p_mount.set_defaults(func=cmd_mount)

    p_un = sub.add_parser("unmount", help="Unmount the virtual filesystem")
    p_un.add_argument("mountpoint")
    p_un.set_defaults(func=cmd_unmount)
    return parser


def cmd_qa_init(args):
    policy = qa.initialize(args.profile, args.local_root)
    print(f"QA profile initialized: {policy.profile}")
    return 0


def cmd_qa_create_folder(args):
    if not qa.current_policy():
        raise qa.QaSafetyError("QA safety stop: select --qa-profile first")
    print(GoogleDriveClient().create_qa_root(args.name))
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = list(sys.argv[1:] if argv is None else argv)
        # Initialization must not discover or write ordinary settings/log files.
        if arguments and arguments[0] == "qa-init":
            args = build_parser().parse_args(arguments)
            return int(args.func(args))
        arguments = qa.bootstrap_profile(arguments)
        args = build_parser().parse_args(arguments)
        qa.check_startup()
        configure_logging()
        return int(args.func(args))
    except KeyboardInterrupt:
        print("Cancelled")
        return 130
    except Exception as exc:
        LOGGER.exception("Command failed")
        print(f"Error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
