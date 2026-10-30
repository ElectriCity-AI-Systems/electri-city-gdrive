# Explicit persistent QA safety mode

The owner requests a dedicated QA environment while normal mode and its six
personal pairs remain untouched. A changed XDG profile alone is not a safety
boundary. This implementation adds one opt-in policy used at all sync/file/API
boundaries. No real Drive operation is part of implementation acceptance.

## Policy and trust

- `qa-init --profile ABS --local-root ABS` explicitly seals a private QA profile
  and an empty dedicated data directory (disjoint from profile/credentials).
- Profile policy and two discovery markers persist across restarts and missing
  activation flags. Settings cannot disable QA or change its allowed root.
- Policy/profile/data identities are checked on every use. Policy alteration
  while a process is running fails closed. Relative paths, traversal, symlink
  components, hardlinked source files, and unsupported filesystem entries fail.
- QA paths never fall back to ordinary XDG settings or the ordinary keyring.
  Existing OAuth files explicitly in the chosen isolated profile remain there;
  no credentials or pairs are copied. Inherited OAuth/scope overrides are ignored.
- Cloud roots start with an empty allowlist. An explicit `qa-create-folder`
  command can create a NEW synthetic folder with a random profile nonce in Drive
  appProperties. Registration binds its immutable ID, exact name and parents.
  Existing folders are never enrolled merely by name or automatically modified.
- A guarded Drive client anchors sync at `qa:<id>`, checks metadata provenance and
  root marking again before operations, rejects out-of-scope IDs and all trash,
  untrash and raw-service access. Normal mode returns the existing client.

## Boundaries

Validate at startup, settings load/save, GUI add/enable/start, queued worker start,
CLI sync entry, both sync engines, planners, hashing and file transfers. Manual
upload/download queues share the same client and path guards. FUSE is disabled
in QA mode. Titles and a permanent visible banner identify QA mode.

Use secure no-follow file opening for QA upload/hash reads; revalidate queued
paths and remote IDs before work. Existing normal-mode codepaths retain their
current behavior. The protection targets app misuse and tampered settings; it
is not an OS sandbox against a same-user attacker who can modify application
code, remove all trusted policy files, or deliberately place personal copies
inside an explicitly approved synthetic data directory.

## Verification plan

1. Policy tests: opt-in/sticky activation, isolation, corrupted/tampered policies,
   absolute/disjoint roots, path escapes, symlink/hardlink attacks and file swaps.
2. Cloud fake tests: ID/nonce/name/parent mismatch, spoofed QA names, reparented
   entries, deletion refusal, raw API refusal and positive synthetic transfers.
3. Integration tests: both engines, configured/background runner, CLI, add/enable/
   sync-all/now GUI, queue-time vs execution-time tampering, startup validation,
   own-profile OAuth/keyring isolation and visible QA marking.
4. Full normal-mode suite (baseline 278), syntax/import checks, independent
   security review, explicit staging and credential scan, commit/push own branch
   and update PR #20. No merge, release, real sync or cloud deletion.

## Safe use

Initialize once with absolute paths, outside the source checkout:

```sh
.venv/bin/python -m electridrive.cli qa-init \
  --profile /absolute/private/qa-profile --local-root /absolute/empty/qa-data
./scripts/run_qa.sh /absolute/private/qa-profile
```

The initializer refuses nonempty data directories, overlapping directories and
profiles containing any sync pairs. It never removes pairs or adopts normal
settings. Use the persistent launcher for every restart. QA discovery also works
when XDG points at the sealed profile, even without the activation option.
An ordinary launch with ordinary XDG paths remains the ordinary installation.

An existing **explicitly chosen, empty isolated** OAuth profile can be sealed in
place; client/token files stay in that profile. They must be private regular files
(mode 0600), without symlinks/hardlinks. Tokens must have only `drive.file` scope.
No keyring, inherited client/scope override, installed private client, or ordinary
profile fallback is used. A newly created profile has no OAuth credentials; sign
in separately using its own client file. Full Drive, Picker and FUSE are disabled.

Cloud targets initially remain unavailable. To deliberately create a new empty
synthetic root later, first close the QA GUI, then explicitly run:

```sh
.venv/bin/python -m electridrive.cli --qa-profile /absolute/private/qa-profile \
  qa-create-folder --name ElectriDrive-QA-artificial
```

This is a real cloud write and is **not** performed by the test suite or launcher.
It prints `qa:<immutable-id>`. Restart the GUI afterward; registration changes in
another process invalidate the running process's pinned registry. Select only
artificial local data and that registered ID. New pairs remain disabled until
explicitly enabled. Deletions stay forbidden in every QA direction.

## State and filesystem protections

QA database operations use an in-memory SQLite schema and parameterized restore
from `state/electridrive/qa-sync-state.json`. Every commit atomically writes that
private JSON snapshot. SQLite never opens the legacy database filename or its
sidecars in QA. A no-follow, descriptor-validated `flock` allows one QA database
owner at a time; close the GUI before CLI sync. Ordinary SQLite persistence is
unchanged. Profile snapshots/locks, settings, logs, OAuth files and capabilities
are local data and must never be committed.

No-follow directory-descriptor traversal protects source reads, hashing, scan,
mkdir, atomic downloads, settings and snapshot access. Downloads compare the
held parent directory identity with the current approved parent before publishing.
Log appends validate the actual file descriptor on every write.

This application policy is not a kernel sandbox. A hostile process with the same
user ID can still race namespace renames after the last identity check while an
already-open descriptor is used; move the application into an OS sandbox if that
threat must be covered. Remote reparenting between metadata validation and a Drive
request likewise has no atomic subtree guarantee. Deliberately placing personal
copies inside an approved synthetic local/cloud tree is outside the data-origin
promise. Tests cover deterministic swaps and stale metadata but do not prove the
absence of every concurrent external filesystem or Drive mutation.
