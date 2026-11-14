# ElectriDrive 2.2.2

## Safer Upload-only duplicate reconciliation

- With **Auto newest** explicitly enabled, Upload-only can bind an existing
  Drive ID when the local file's checksum and size exactly match one of multiple
  fully scanned, verified binary copies. This is a no-write reconciliation:
  no duplicate ID is deleted or updated, and differing remote contents remain
  available.
- When no duplicate exactly matches local content, automatic updates still
  require the local file to be unambiguously newer than every verified binary
  duplicate. A newer remote copy, tied/unknown timestamps, native Google
  Workspace files, incomplete inventories, and changed identities remain
  blocked before transfers for manual review.
- Repeated syncs use the recorded Drive ID and do not create new files.

## Upgrade and access limits

Install the verified Debian package over the existing installation, or replace
the AppImage with the verified candidate. Keep the user's configuration and
sync-state directories. Existing sync pairs and their explicit Auto newest
setting are retained.

The default `drive.file` scope does not guarantee visibility of arbitrary
pre-existing Drive contents. A successful scan only covers files the
authorized client can see; exact-folder access or full Drive scope may be
required before claiming a complete backup. Native Google Workspace documents
cannot be reconciled by binary checksum.

This is a release candidate pending independent package, installation, and
owner-authorized live-drive verification. It does not replace or move the
existing v2.2.1 pre-release.
