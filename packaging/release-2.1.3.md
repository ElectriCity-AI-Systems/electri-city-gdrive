This release makes interrupted downloads safer and fixes Linux desktop/runtime
compatibility. It includes the Debian launcher correction from v2.1.2.

## Changes

- Failed or cancelled downloads and Workspace exports preserve an existing local
  destination. Successful transfers replace it atomically and retain permissions.
- Debian installs the FUSE 2 runtime required by fusepy and a mount helper.
- Host GLib/GIO libraries prevent the dconf/GVfs symbol errors observed on newer
  Ubuntu desktops.
- Bundled Python is now 3.12.15; Linux compatibility remains GLIBC 2.35.
- The desktop application identity is initialized before Qt creates the app.
- electridrive --doctor reports Python, FUSE and profile information.

## Download and install

Download **electridrive_2.1.3_amd64.deb** and **SHA256SUMS.txt** from this release.
In their download directory:

    sha256sum -c SHA256SUMS.txt &&
    sudo apt install ./electridrive_2.1.3_amd64.deb

Launch ElectriDrive from the application menu or run electridrive.

## Validation

- The five Python 3.10–3.14 test jobs and Debian package/desktop checks on
  Ubuntu 22.04 and 24.04 must pass on this exact release commit before publication.
- The public package reuses the successful CI artifact, adds the unchanged
  built-in app OAuth client, and verifies that all original payload bytes and
  effective permissions are preserved.
- Package verification checks version, desktop launcher, Qt, declared
  dependencies, the built-in client and GLIBC maximum 2.35. A fresh isolated
  profile passes Python/FUSE diagnostics, host-GIO and an offscreen GUI launch.
- The owner completed the agreed practical tests on Ubuntu 26.04 with private
  candidate 3 and confirmed that a donation correctly delivers a PRO license key.
  Real upload and download completion are also recorded in the supplied logs.
  The final version update changes release metadata, not the tested app behavior.

The built-in client is unchanged from the existing public distribution. No user
tokens, personal settings or private license-signing keys are included.

A direct Kubuntu/KDE test remains the reporter's confirmation on issue #5.
The ATK bridge warning seen in the Ubuntu test did not prevent the completed
transfer. Virtual Drive remains read-only.

This release contains the current **Debian package**. The separate
[v2.1.1 AppImage](https://github.com/ElectriCity-AI-Systems/electri-city-gdrive/releases/tag/v2.1.1)
is older and does not contain these changes.

ElectriDrive remains free and open source. Supporters can donate what they want
through the application's **Support & get Pro** link.
