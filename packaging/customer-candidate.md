# Customer candidate gate

The public `v2.1.3` release is unchanged. Its original publication script
(`scripts/publish_release_2_1_3.py`) added an embedded Desktop OAuth client
from an independently checksum-verified older public package. That established
release is **not** equivalent to a credential-free CI artifact. Whether the
published client still works with a new Google account must be tested against
the exact public binary; no inference of a missing client from a QA build.

Normal CI output is watermarked
`CI-QA — NOT FOR CUSTOMERS`; it is not a customer installer. A
`CUSTOMER-CANDIDATE` build is not a release and is never published by the
candidate workflow.

## Owner setup

Before building, the publisher must:

1. Create a Google Cloud project, enable the Google Drive API and Picker API,
   create an OAuth client of type **Desktop app**, and configure the consent
   screen, requested `drive.file` scope, test users, and publishing status for
   the intended audience. Resolve any Google verification or quota requirements.
2. Create a separate Google API key for Picker, apply the narrowest applicable
   application restriction for this desktop client and restrict API use to
   Picker. Confirm the restriction works in the installed application.
3. Create the `customer-candidate` GitHub environment, allow deployments only
   from `main`, and require a named owner reviewer. Add the Desktop OAuth JSON
   (including `installed` fields and `picker_api_key`) as the
   `ELECTRIDRIVE_APP_CLIENT_JSON` environment secret. Add the standalone key as
   `ELECTRIDRIVE_PICKER_API_KEY` only when the JSON does not already contain it.
4. Separately provision a `stable-release` GitHub environment with main-only
   deployment restrictions, required owner approval, and the least-privilege
   protected release credential needed by the future publication process. No
   workflow in this change uses that credential, and the candidate workflow has
   no publishing permission.

The workflow is manually dispatched from `main` and can read secrets only after
the protected environment approves the job. It writes the JSON to a mode-0600
runner temporary file for the build, deletes that file on step exit, never
prints its contents, and never uploads that source file or OAuth tokens. The
finished customer candidate necessarily embeds the public Desktop client config;
it remains a short-lived Actions artifact, not a GitHub Release.

The workflow builds the `.deb` and AppImage once from the same source commit on
Ubuntu 22.04 / GLIBC 2.35. It verifies the channel, clean-source commit
provenance, embedded OAuth/Picker shape, absence of the QA marker, and the
SHA256SUMS file before installing those exact bytes. It runs `--doctor` with a
fresh XDG profile and checks that both OAuth and Picker resolve from the
package, then launches both exact artifacts in a GUI smoke test. No developer
JSON or CLI OAuth setup is used. Automation does not contact Google or perform
real Drive operations.

## Release remains blocked

The following are owner-only gates; none is inferred from CI:

1. Verify Google OAuth consent-screen readiness for the exact publisher
   project/client and confirm the Picker API key restrictions with a real
   Google account.
2. Download the candidate artifacts and `SHA256SUMS.txt` from the same workflow
   run. In one directory, run `sha256sum --check SHA256SUMS.txt`; record the
   workflow run, source commit, both artifact names, and hashes alongside the
   acceptance result. Do not accept artifacts whose hashes do not match.
3. On an **actual clean Ubuntu 26.04** desktop with no developer OAuth files,
   install and test each exact artifact (`.deb` and AppImage) with a fresh
   profile. Complete Google sign-in and Picker folder selection. In a
   publisher-owned synthetic test folder, create the transfer payload:

   ```bash
   python3 -c 'from pathlib import Path; Path("/tmp/electridrive-45-byte.bin").write_bytes(bytes(range(45)))'
   test "$(wc -c < /tmp/electridrive-45-byte.bin)" -eq 45
   sha256sum /tmp/electridrive-45-byte.bin
   ```

   Upload it, download it, and compare with `cmp` and `sha256sum`. Restart and
   reconnect, then verify the app still opens and Picker works. Keep test
   content synthetic and do not use customer data.
4. Verify no unintended deletion, test the failure/retry path, and record the
   result against these exact artifact hashes. A QA run, prior release, CI GUI
   launch, or successful package install is not a substitute.
5. Obtain separate owner approval for any stable release credentials, tag,
   GitHub Release, updater announcement, or public customer download. This task
   authorizes none of them.

The workflow pins and SHA256-verifies the versioned AppImage build tool (1.9.1)
and runtime (20251108) against upstream asset digests. If pinned artifacts
become unavailable or a checksum changes, the build stops rather than
downloading or executing an unverified replacement.

The workflow does not sign packages; if publisher signing is required, the owner
must provision the signing process before distribution.

## Rejection and rollback

If a candidate fails any gate, do not tag or publish it. Preserve its run ID and
hashes for diagnosis, remove the candidate Actions artifact, uninstall the
package with `sudo apt remove --purge electridrive`, and remove only the
publisher-created synthetic test file from the test folder. Revoke the test
Google consent and rotate the Picker key/client if the publisher believes the
credentials were exposed. Do not delete unrelated Drive items.

If a later, separately approved stable release must be rolled back, disable the
GitHub Release and its downloads immediately, restore the previous approved
stable download/update target, and publish a corrected candidate only after
repeating the owner acceptance above. Keep the failed artifact hashes and
incident record; never silently replace bytes under an existing release hash.
