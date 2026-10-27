#!/usr/bin/env bash
# Build a .deb that installs ElectriDrive into /opt (self-contained PyInstaller bundle).
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PY:-.venv/bin/python}"
MAX_GLIBC_VERSION="${MAX_GLIBC_VERSION:-2.35}"

"$PY" -c 'import PyInstaller' >/dev/null || {
  echo "PyInstaller is missing; install packaging/requirements-release.txt first." >&2
  exit 1
}

# An unconfigured package must never be distributed to customers.
# ALLOW_PLACEHOLDER_CLIENT=1 is exclusively for CI's non-distributable launch test.
BAKED=electridrive/google_api/client_baked.json
QA_ONLY=0
umask 077
trap 'rm -f "$BAKED"' EXIT
rm -f "$BAKED"
if [ -n "${ELECTRIDRIVE_CLIENT_ID:-}" ]; then
  "$PY" scripts/bake_client.py
elif [ -n "${ELECTRIDRIVE_APP_CLIENT_FILE:-}" ] && [ -f "$ELECTRIDRIVE_APP_CLIENT_FILE" ]; then
  cp "$ELECTRIDRIVE_APP_CLIENT_FILE" "$BAKED"
  echo "Using provided OAuth client (values not displayed)"
elif [ "${ALLOW_PLACEHOLDER_CLIENT:-0}" = "1" ]; then
  QA_ONLY=1
  echo "WARNING: CI QA-only package: no OAuth, NOT FOR CUSTOMERS" >&2
else
  echo "RELEASE BLOCKED: configure ELECTRIDRIVE_APP_CLIENT_FILE with Desktop OAuth + Picker key." >&2
  echo "Customer packages must support one-click Google sign-in." >&2
  exit 1
fi

if [ "$QA_ONLY" -eq 0 ]; then
  "$PY" scripts/validate_distribution_oauth.py "$BAKED"
fi

rm -rf build dist
"$PY" -m PyInstaller --noconfirm packaging/electridrive.spec
rm -f "$BAKED"   # captured into the build; keep source clean

VERSION="$("$PY" -c 'from electridrive.config import APP_VERSION; print(APP_VERSION)')"
ROOT="dist/deb/electridrive_${VERSION}_amd64"
rm -rf "$ROOT"
mkdir -p "$ROOT/opt/electridrive" "$ROOT/usr/bin" \
         "$ROOT/usr/share/applications" \
         "$ROOT/usr/share/icons/hicolor/256x256/apps" "$ROOT/DEBIAN"

cp -r dist/electridrive/* "$ROOT/opt/electridrive/"
ln -sf /opt/electridrive/electridrive "$ROOT/usr/bin/electridrive"
cp assets/electridrive.png "$ROOT/usr/share/icons/hicolor/256x256/apps/electridrive.png"
cp packaging/electridrive.desktop "$ROOT/usr/share/applications/electridrive.desktop"
if [ "$QA_ONLY" -eq 1 ]; then
  printf '%s\n' "CI QA-only: this package lacks Google OAuth and MUST NOT be distributed." > \
    "$ROOT/opt/electridrive/QA-NOT-FOR-DISTRIBUTION.txt"
fi

cat > "$ROOT/DEBIAN/control" <<EOF
Package: electridrive
Version: ${VERSION}
Section: net
Priority: optional
Architecture: amd64
Depends: libfuse2t64 | libfuse2, fuse3 | fuse, libglib2.0-0t64 | libglib2.0-0, libegl1, libgl1, libwayland-cursor0, libwayland-egl1
Maintainer: Pierre Stephan / Electri_C_ity Studios
Description: ElectriDrive - Electric-City Drive for Linux
 Beautiful, safety-first Google Drive client: browse, up/download,
 two-way sync and a FUSE files-on-demand mount. Without rclone.
EOF

dpkg-deb --build --root-owner-group "$ROOT"
VERIFY_ARGS=("$ROOT.deb" --expected-version "$VERSION" --max-glibc "$MAX_GLIBC_VERSION")
if [ "$QA_ONLY" -eq 0 ]; then
  VERIFY_ARGS+=(--require-baked-client)
fi
"$PY" scripts/verify_deb.py "${VERIFY_ARGS[@]}"
if [ "$QA_ONLY" -eq 1 ]; then
  echo "QA-ONLY (not publishable): ${ROOT}.deb"
else
  echo "Customer release candidate: ${ROOT}.deb"
fi
