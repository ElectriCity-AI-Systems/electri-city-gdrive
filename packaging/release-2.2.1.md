# ElectriDrive 2.2.1 — release candidate

Supersedes the 2.2.0 candidate with a data-safety fix found during its owner-led
acceptance pass, plus the packaging fix that made that acceptance pass possible.

- **Cross-pair Drive-ID ownership.** A name/path match at an upload target was
  treated as ownership in both the upload-only engine and the two-way engine's
  `up_only` path: a different sync pair uploading a same-named file could
  silently overwrite an unrelated, already-tracked file. A remote_id is now
  only trusted as an update target when this pair's own history recorded it,
  or when no pair has ever claimed it (a brand-new pair may still adopt
  genuinely untracked pre-existing Drive content on its first sync, as
  before). A stale tracked ID that is gone and replaced by an unrelated file
  now fails that file closed instead of guessing; a tracked ID that is merely
  renamed/moved elsewhere but still alive is followed by ID.
- **CLI dispatch.** The packaged (PyInstaller) binary recognized only the
  literal `--doctor` flag; every other documented subcommand (`list`, `sync`,
  `sync-up`, `qa-init`, `qa-create-folder`, …) silently started the GUI
  instead. This predates 2.2.0 (identical in the published v2.1.3) and
  explains CLI invocations that appeared to do nothing. Fixed: any non-empty
  argument set now reaches the real CLI; `--doctor` is unchanged.
- **Full-backup consent.** Enabling a sync pair with full backup on
  (`include_all_files`, the default) now says so in the confirmation dialog
  itself — `.env` files, private keys and other credentials are included
  unencrypted, not just in the README.

Carried over from the 2.2.0 candidate: account/root-bound synchronization history,
Changes-feed-gated inventory reuse, per-account transfer history with required
manual retry after interruption, FUSE cache validation, hardened token lifecycle
and Picker callbacks, and SMTP license-delivery recovery. See git log for detail.

This is a draft candidate. Do not mark it Latest until the exact packages have passed
owner-led real-account acceptance. Automated checks do not establish successful live
Google sign-in, personal sync behavior or production SMTP delivery.

Install the Debian package only after checking SHA256SUMS.txt. AppImage users should
verify the same checksum file and make the AppImage executable. Both formats must
come from this same candidate commit; see candidate-manifest.json for provenance.
