# Verified release candidates

Work on a dedicated integration branch and preserve the full upstream QA/test suite.
Commit all source changes before building. Five Python jobs and both Debian desktop
jobs must pass on the exact candidate commit.

Build from a clean checkout with the explicitly approved Desktop OAuth configuration:

```bash
ELECTRIDRIVE_APP_CLIENT_FILE=/absolute/path/approved-client.json bash scripts/build_candidate_in_container.sh
```

The wrapper snapshots tracked source before the build, uses the pinned Ubuntu 22.04
container, verifies both customer formats and GLIBC maximum 2.35, then records asset
hashes and exact source provenance in dist/candidate-manifest.json. The public client
may be recovered from v2.1.3 only after validating the published package and client hashes.
Never use personal tokens, profiles or private license keys as publisher config.

After pushing and obtaining a successful CI run for the exact commit:

```bash
python3 scripts/prepare_release_draft.py --ci-run VERIFIED_RUN_ID
```

This command rechecks source/assets, all seven jobs and version ordering. It creates
only a draft and validates uploaded bytes. It never publishes or changes Latest.
The manual release-draft workflow provides the same process for a trusted main commit.
Existing drafts are not overwritten. Preserve the old v2.1.3 workflow as historical code.

Before publication, perform owner-led sign-in and synthetic transfer/sync/conflict
acceptance with the exact candidate packages. Confirm both public download hashes,
Git tag commit, update detection and install/desktop launch on supported Ubuntu targets.
