# Customer candidate gate

The public `v2.1.3` release is unchanged. Normal CI output is watermarked
`CI-QA — NOT FOR CUSTOMERS`; it is not a customer installer. A
`CUSTOMER-CANDIDATE` build is not a release and is never published by the
candidate workflow.

## Owner setup

The repository owner must create the `customer-candidate` GitHub environment,
restrict it to `main`, and configure required owner approval before deployment.
Store the publisher-provisioned Desktop OAuth JSON in the
`ELECTRIDRIVE_APP_CLIENT_JSON` environment secret and the Google Picker key in
`ELECTRIDRIVE_PICKER_API_KEY`. The client must use Google's expected OAuth
endpoints and HTTP loopback redirects. These values are consumed by the
workflow as environment secrets and are not written to logs or source files.
The build validates the config and verifies it again inside both finished
artifacts.

Manually dispatch **Customer candidate (manual, no publishing)** from `main`.
The workflow builds on Ubuntu 22.04 / GLIBC 2.35, checks the `.deb` and
AppImage payloads against that baseline, and launches both exact artifacts
headlessly and under Xvfb. It uploads a short-lived Actions artifact only; it
does not create or modify a GitHub release. The protected environment and its
required reviewer must be configured by the owner—workflow YAML cannot
configure those repository-side approval rules.

## Release remains blocked

CI can verify packaging and credential structure, but cannot approve Google's
consent screen, scopes, or demonstrate a real customer's account. Before
distribution, the owner must manually install the exact candidate on a clean
Ubuntu 26.04 system and use a real Google account to accept consent, exercise
Picker, upload and download, restart and reconnect, safely resolve a duplicate
without deleting the other item, and complete a full-file backup including a
hidden file. Record results for that exact artifact. Automated CI or prior
Drive acceptance of another build is not a substitute. Do not publish a
release or change versions until the owner approves the exact-binary acceptance.
The workflow does not sign packages; if publisher signing is required, the owner
must provision the signing process before distribution.
