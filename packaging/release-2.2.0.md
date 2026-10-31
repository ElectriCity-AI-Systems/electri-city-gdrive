# ElectriDrive 2.2.0 — release candidate

This candidate integrates the current updater, explicit Drive-path conflict review and
isolated QA safeguards with additional synchronization, credential and delivery protections.

- Bind synchronization history to account and root identity; preserve live local edits,
  unique recovery copies and stable remote IDs. Stop on unavailable or partial inventories.
- Reuse verified remote inventories only after an empty complete Changes feed; QA mode
  retains its restricted inventory path and never uses account-global Changes access.
- Coordinate session jobs and shutdown, retain transfer history per account, and require
  explicit retry for interrupted uploads. Preserve QA execution-time path checks.
- Validate FUSE caches and propagate mount failures. Virtual Drive remains read-only.
- Harden token lifecycle and Picker callbacks; persist license-delivery tasks with safe
  retries and explicit recovery for ambiguous SMTP outcomes.
- Build both formats on Ubuntu 22.04 / GLIBC 2.35 with the unchanged approved public
  Desktop OAuth client. Personal tokens, profiles and signing keys are not included.

This is a draft candidate. Do not mark it Latest until the exact packages have passed
owner-led real-account acceptance. Automated checks do not establish successful live
Google sign-in, personal sync behavior or production SMTP delivery.

Install the Debian package only after checking SHA256SUMS.txt. AppImage users should
verify the same checksum file and make the AppImage executable. Both formats must
come from this same candidate commit; see candidate-manifest.json for provenance.
