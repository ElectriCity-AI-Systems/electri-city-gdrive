# ElectriDrive finalization implementation plan

> Execute inline using systematic debugging, test-driven development and final review.

**Goal:** Provide the owner's tested 2.1.3 checkout, commit, branch and PR.
**Architecture:** Existing sync engines, Google API client and PySide6 UI; minimal fixes.
**Tech Stack:** Python 3.10+, PySide6, pytest, Google Drive v3, Debian/PyInstaller.
**Spec:** docs/superpowers/specs/2026-10-29-electridrive-final-design.md

## Global constraints

No personal sync pairs, cloud deletion, main merge, release, credentials or profiles
in Git. Preserve pre-existing files. Cloud checks require validated synthetic QA.

## Review focus

Ambiguous parent folders; multiple binary IDs across pages; edited Workspace
exports in down-only mode; repeated pagination tokens; stale reviewed IDs after
restart. Check those explicitly in tests and final review.

## Tasks

- [x] Verify repository, clean reference, current main and PR #19; create linked
  worktree and isolated branch. Baseline: 249 tests pass on Python 3.14.4.
- [x] Add regression tests in tests/test_drive_client.py for duplicate folders and
  binary IDs (including pagination). Run RED; minimally fix find_folder/find_file
  in electridrive/google_api/client.py to refuse ambiguity; run GREEN.
- [x] Add regression in tests/test_twoway.py for down-only Workspace conflict:
  original local bytes survive in a conflict copy, remote remains unchanged and
  upload/update/trash counts stay zero. Run RED; fix _apply_conflict; run GREEN.
- [x] Test repeating listing/change pagination; fail visibly in sync scanners
  and manual download planning. Verify failure prevents transfers.
- [x] Run complete suite, compile/import checks, isolated GUI offscreen/XCB/
  native Wayland and doctor; build/verify Debian if locally available. Assess
  existing QA fixture without changing original profile or personal files.
- [x] Review full diff, staged paths and secrets; document exact evidence and
  limits. Publication proceeds with a documentation commit, verified remote,
  own-branch push and a draft PR; record those returned IDs in the final report.

## Execution record

- Additional RED/GREEN fix: download-only source resolution never creates missing Drive folders.
- Ruling: an optional Changes-feed cycle terminates that feed and falls back to a full scan; required tree listings fail the run. This preserves existing API-failure recovery.
- Final independent review: 90 focused tests passed; QA restore validation moved before writes with RED/GREEN regression and full green suite (262 tests).
- Deferred minor: persistent stale-ID restart coverage passed independent ad-hoc checks, but is not yet a dedicated committed regression test.
- Live QA: initial two-file preflight stopped before writes; verified synthetic media completion, then stale-ID safety PASS and successful restoration. No cloud deletions or personal sync.

- Package verification: Debian build/GLIBC 2.35, Python 3.12.15 doctor, host GIO, token-free package scan and packaged Offscreen/XCB/Wayland startup all passed. Full 262-test suites passed on Python 3.14.4, 3.12.15 and older 3.10.12 (one upstream support warning only on 3.10).

- Additional verified scope: five actual Dependabot alerts found at push, covering pinned urllib3 2.7.0 and oauthlib 3.3.1. Upgrade to advisory-patched 2.8.0 / 4.0.0; repeat release suite/build/package verification. Main alerts remain until a separately approved integration.

- Final security validation: 262 tests passed in native patched Python-3.12.15 release image and fresh Python-3.14.4 venv. Actual PYZ versions confirmed (urllib3 2.8.0 / oauthlib 4.0.0); rebuilt Debian package passed GLIBC/doctor/GIO/profile scans and all three GUI startup modes again. No new cloud operations needed; live QA already used the same patched urllib3/oauthlib in the host venv.
