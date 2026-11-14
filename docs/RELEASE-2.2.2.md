# ElectriDrive 2.2.2 — proposed release

This is a release proposal, not a published release. Keep the existing public
v2.2.1 pre-release and its assets unchanged. Publish a new `v2.2.2` tag only
after independent review and the checks below; do not use this change to publish
artifacts or access publisher credentials.

## What changes

For an explicitly opted-in Upload-only pair, an exact local checksum-and-size
match to one of multiple fully inventoried, verified binary Drive files permits
the app to bind that existing ID without changing any Drive content. Other
duplicate objects, including different remote contents, remain intact. A repeat
sync uses the saved ID and makes no additional writes.

If no duplicate exactly matches local content, automatic in-place updates still
require an unambiguous timestamp ordering with the local file strictly newer
than every verified binary candidate. A newer remote copy, tied/unknown
timestamps, native Google Workspace content, missing metadata, concurrent
changes, folder/name collisions, or an incomplete inventory remains a
pre-transfer blocker for manual review. Upload-only never deletes a duplicate.

## Upgrade path

- **Debian:** verify the published checksum, then install
  `electridrive_2.2.2_amd64.deb` with the package manager. It upgrades the
  existing installation; user settings and sync state are not package assets
  and must remain in the user's configuration/state directories.
- **AppImage:** verify the checksum and replace the old AppImage file with the
  new one. Do not delete the user's configuration or sync-state directories.
- After upgrading, confirm the intended sync pair is enabled and
  `Auto newest` remains explicitly on only for pairs the owner approves. Review
  any stored/previously selected Drive IDs before changing permissions or the
  selected remote folder.

## Scope and completeness limits

The default `drive.file` OAuth scope does not promise visibility of arbitrary
legacy files in a pre-existing Drive folder. A successful scan covers only the
inventory the authorized client can actually see. Do not report a complete
backup of an existing folder until exact-folder access and full traversal are
verified. Full Drive scope or a Picker grant may be required. Native Google
Docs/Sheets/Slides do not expose binary checksums and cannot be auto-reconciled
by this duplicate algorithm.

## Independent release verification checklist

- [ ] Review and merge only after the synthetic regression suite and CodeQL/CI
  checks are green; record the exact reviewed source revision.
- [ ] From a clean checkout, build a `CUSTOMER-CANDIDATE` with publisher-owned
  OAuth Desktop and Picker configuration. Verify provenance and both artifacts
  with `scripts/verify_customer_artifact.py`; verify Debian metadata and
  checksums with `scripts/verify_deb.py`.
- [ ] Install and launch the Debian package through the project CI gates on the
  supported Ubuntu baselines. Install and launch the AppImage in a clean,
  isolated profile; confirm version, UI, and settings migration.
- [ ] With an owner-authorized test account and synthetic Drive folder, verify
  exact-match no-op, duplicate ID preservation, repeated-sync idempotence,
  blocked ambiguous/newer remote cases, and partial-failure reporting. Confirm
  that `drive.file`-limited listings never claim unobserved content was backed
  up.
- [ ] Have the owner review the live-drive result, candidate checksums, release
  notes, and tag/asset plan. Only then create the distinct `v2.2.2` release.

No live Drive access, owner profile inspection, credential use, tag movement, or
publishing was performed for this proposal. Until those independent checks are
complete, a customer-candidate artifact and public release remain hard
blockers.
