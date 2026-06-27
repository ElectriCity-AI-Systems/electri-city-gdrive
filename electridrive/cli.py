from __future__ import annotations

import argparse
import logging
from pathlib import Path

from electridrive.config import SyncPair, get_paths, selected_scopes
from electridrive.google_api.client import GoogleDriveClient
from electridrive.logging_setup import configure_logging
from electridrive.storage.database import SyncDatabase
from electridrive.sync.downloader import plan_download
from electridrive.sync.engine import UploadOnlySyncEngine

LOGGER = logging.getLogger(__name__)


def cmd_doctor(args: argparse.Namespace) -> int:
    from electridrive.vfs import fuse_available

    paths = get_paths()
    print("ElectriDrive doctor")
    print(f"Config dir:       {paths.config_dir}")
    print(f"State dir:        {paths.state_dir}")
    print(f"Cache dir:        {paths.cache_dir}")
    print(f"Credentials file: {paths.credentials_file} {'OK' if paths.credentials_file.exists() else 'MISSING'}")
    print(f"Database file:    {paths.database_file}")
    print(f"OAuth scopes:     {', '.join(selected_scopes())}")
    print(f"FUSE available:   {'yes' if fuse_available() else 'no'}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    client = GoogleDriveClient()
    files = client.list_files(limit=args.limit)
    for item in files:
        print(f"{item.id}\t{item.name}\t{item.mime_type or ''}\t{item.size or ''}")
    return 0


def cmd_sync_up(args: argparse.Namespace) -> int:
    paths = get_paths()
    db = SyncDatabase(paths.database_file)
    try:
        engine = UploadOnlySyncEngine(
            drive_client=GoogleDriveClient(),
            database=db,
            log_callback=lambda msg: print(msg),
        )
        result = engine.sync_up(Path(args.local_folder), args.remote_folder)
        return 0 if result.failed == 0 else 2
    finally:
        db.close()


def cmd_sync(args: argparse.Namespace) -> int:
    from electridrive.sync.twoway import TwoWaySyncEngine

    paths = get_paths()
    db = SyncDatabase(paths.database_file)
    try:
        pair = SyncPair(local_path=str(Path(args.local_folder).expanduser()),
                        remote_folder=args.remote_folder, direction=args.direction)
        engine = TwoWaySyncEngine(GoogleDriveClient(), db, pair, log_cb=print,
                                  deep_verify=getattr(args, "deep_verify", False))
        report = engine.run()
        print(f"Done. up={report.uploaded} down={report.downloaded} "
              f"trash_remote={report.trashed_remote} trash_local={report.trashed_local} "
              f"conflicts={report.conflicts} skipped={report.skipped} failed={report.failed}")
        if getattr(args, "verbose", False):
            print(f"  diagnostics: hashed={report.hashed} "
                  f"remote_source={'full-scan' if report.full_remote_scan else 'incremental-cache'}")
        for err in report.errors:
            print(f"  ! {err}")
        return 0 if report.failed == 0 else 2
    finally:
        db.close()


def cmd_download(args: argparse.Namespace) -> int:
    client = GoogleDriveClient()
    remote = client.get_metadata(args.file_id)
    dest = Path(args.dest).expanduser()
    items = plan_download(client, remote, dest)
    failed = 0
    for it in items:
        try:
            if it.is_google_doc:
                client.export_file(it.file_id, it.dest_path, it.export_mime)
            else:
                client.download_file(it.file_id, it.dest_path)
            print(f"downloaded {it.dest_path}")
        except Exception as exc:
            failed += 1
            print(f"FAILED {it.name}: {exc}")
    return 0 if failed == 0 else 2


def cmd_mount(args: argparse.Namespace) -> int:
    from electridrive.vfs import FuseMount, fuse_available

    if not fuse_available():
        print("FUSE is not available (need libfuse + fusermount).")
        return 1
    paths = get_paths()
    mount = FuseMount(GoogleDriveClient(), paths.vfs_cache_dir)
    mount.start(args.mountpoint, args.remote_folder or "", writable=args.writable)
    print(f"Mounted Drive at {args.mountpoint}. Press Ctrl+C to unmount.")
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
    subprocess.run([tool, "-u", args.mountpoint], check=False)
    print(f"Unmounted {args.mountpoint}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="electridrive", description="ElectriDrive — Electric-City Drive for Linux")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="Show local configuration status").set_defaults(func=cmd_doctor)

    p_list = sub.add_parser("list", help="List Drive files")
    p_list.add_argument("--limit", type=int, default=20)
    p_list.set_defaults(func=cmd_list)

    p_up = sub.add_parser("sync-up", help="Upload-only sync (safe, never deletes)")
    p_up.add_argument("local_folder")
    p_up.add_argument("--remote-folder", required=True)
    p_up.set_defaults(func=cmd_sync_up)

    p_sync = sub.add_parser("sync", help="Two-way sync a local folder with Drive")
    p_sync.add_argument("local_folder")
    p_sync.add_argument("--remote-folder", required=True)
    p_sync.add_argument("--direction", choices=["two_way", "up_only", "down_only"],
                        default="two_way")
    p_sync.add_argument("--deep-verify", action="store_true",
                        help="always re-hash local files (ignore the mtime/size fast-path)")
    p_sync.add_argument("--verbose", action="store_true",
                        help="print extra diagnostics (files hashed, remote source)")
    p_sync.set_defaults(func=cmd_sync)

    p_dl = sub.add_parser("download", help="Download a file/folder by Drive file id")
    p_dl.add_argument("file_id")
    p_dl.add_argument("dest")
    p_dl.set_defaults(func=cmd_download)

    p_mount = sub.add_parser("mount", help="Mount Drive as a virtual filesystem (no rclone)")
    p_mount.add_argument("mountpoint")
    p_mount.add_argument("--remote-folder", default="")
    p_mount.add_argument("--writable", action="store_true", help="experimental")
    p_mount.set_defaults(func=cmd_mount)

    p_un = sub.add_parser("unmount", help="Unmount the virtual filesystem")
    p_un.add_argument("mountpoint")
    p_un.set_defaults(func=cmd_unmount)
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
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
