# Changelog

## 2.1.1 — 2026-08-24

### Changed

- Production Linux AppImage and Debian builds use the built-in Google Desktop OAuth client,
  allowing end users to sign in without creating their own `credentials.json`.
- Version bumped from 2.1.0 to 2.1.1 so existing 2.1.0 installations can discover this
  production distribution through ElectriDrive's update checker.
- No synchronization, licensing, or data-format compatibility changes.

## 2.1.0 — 2026-08-24

### Fixed

- Upload-only and two-way sync now update an existing remote file by its recorded Drive ID
  instead of creating a duplicate. Local-winner conflicts update the same canonical ID.
- Drive folder paths are resolved from My Drive root, preventing the first component from
  matching a same-named folder elsewhere.
- Virtual Drive cache entries are invalidated by `md5Checksum`, or `modifiedTime` when a
  checksum is unavailable. Workspace exports use the same remote-aware invalidation.
- Native Docs, Sheets, and Slides are no longer skipped by two-way sync.

### Added

- Safe, remote-authoritative Workspace exports with stable Office extensions, conflict-copy
  preservation for local edits, and deterministic filename-collision handling.
- Pair-scoped Changes API cursor persistence. A verified no-change feed may reuse a complete
  in-memory snapshot; every unsafe, changed, expired-token, or unavailable case full-scans.
- Asynchronous stable-release notifications using only the official public GitHub Releases API.
  Automatic checks are throttled to once per 24 hours, support per-version dismissal, and fail
  silently when offline. Settings → About also provides an explicit manual check.
- GitHub Actions CI for Python 3.10 through 3.14, running pytest, compileall, and import smoke
  checks without Google credentials or other secrets.

### Changed

- Virtual Drive is explicitly read-only in 2.1.0. The UI no longer advertises writes, legacy
  `--writable` requests fail before mounting, and no unsafe partial-write path is exposed.
- Update downloads remain manual: ElectriDrive opens the canonical GitHub release page and
  never downloads or executes release binaries.

### Privacy and migration

- Update checks send no telemetry, device identifiers, Google account data, license keys, or
  usage statistics; they retrieve only public release metadata.
- ElectriDrive 2.0.0 had no updater and cannot notify users about 2.1.0. After users install
  2.1.0 manually, later stable releases can be announced automatically.

### Known limitations

- A fresh two-way sync engine still performs a full remote scan because the persisted database
  is not a complete remote-tree cache; Changes API optimization is currently same-process only.
- Workspace synchronization is export-to-local only. Edited exports are preserved as conflict
  copies but are never imported back into native Google documents.
- Virtual Drive remains read-only; updates are never downloaded or installed automatically.
