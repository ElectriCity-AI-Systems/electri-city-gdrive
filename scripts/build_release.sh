#!/usr/bin/env bash
# Build both candidate distributables from one PyInstaller run and write checksums.
#
# Customer candidates require a validated publisher Desktop OAuth client and Picker key.
# CI-QA output is watermarked and contains no customer config. Never bundle user tokens,
# the license signing key, or user settings.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PY:-.venv/bin/python}"
VERSION="$("$PY" -c 'from electridrive.config import APP_VERSION; print(APP_VERSION)')"
MAX_GLIBC_VERSION="${MAX_GLIBC_VERSION:-2.35}"
BUILD_CHANNEL="${ELECTRIDRIVE_BUILD_CHANNEL:-CUSTOMER-CANDIDATE}"

"$PY" -c 'import PyInstaller' >/dev/null || {
  echo "PyInstaller is missing; install packaging/requirements-release.txt first." >&2
  exit 1
}

BAKED=electridrive/google_api/client_baked.json
CHANNEL_FILE=electridrive/build_channel.txt
BAKED_CLIENT=0
trap 'rm -f "$BAKED" "$CHANNEL_FILE"' EXIT
rm -f "$BAKED"
if [ "$BUILD_CHANNEL" = "CI-QA" ]; then
  printf '%s\n' "$BUILD_CHANNEL" > "$CHANNEL_FILE"
elif [ "$BUILD_CHANNEL" = "CUSTOMER-CANDIDATE" ]; then
  if { [ -n "${ELECTRIDRIVE_CLIENT_ID:-}" ] && [ -n "${ELECTRIDRIVE_CLIENT_SECRET:-}" ]; } \
      || { [ -n "${ELECTRIDRIVE_APP_CLIENT_FILE:-}" ] && [ -f "$ELECTRIDRIVE_APP_CLIENT_FILE" ]; }; then
    "$PY" scripts/bake_client.py
    BAKED_CLIENT=1
    printf '%s\n' "$BUILD_CHANNEL" > "$CHANNEL_FILE"
  else
    echo "Customer candidate build requires a publisher-provided OAuth Desktop client and Picker key." >&2
    exit 1
  fi
else
  echo "Unsupported build channel: $BUILD_CHANNEL" >&2
  exit 1
fi

rm -rf build dist
"$PY" -m PyInstaller --noconfirm packaging/electridrive.spec
rm -f "$BAKED"   # captured into the build; keep source tree clean

# ---- AppImage ----
APPDIR=dist/ElectriDrive.AppDir
rm -rf "$APPDIR"; mkdir -p "$APPDIR/usr/bin"
cp -r dist/electridrive/* "$APPDIR/usr/bin/"
cp assets/electridrive.png "$APPDIR/electridrive.png"
printf '[Desktop Entry]\nType=Application\nName=ElectriDrive\nExec=electridrive\nIcon=electridrive\nCategories=Network;FileTransfer;Utility;\nTerminal=false\n' > "$APPDIR/electridrive.desktop"
printf '#!/bin/bash\nHERE="$(dirname "$(readlink -f "$0")")"\nexec "$HERE/usr/bin/electridrive" "$@"\n' > "$APPDIR/AppRun"
chmod +x "$APPDIR/AppRun"
TOOL=build/appimagetool-x86_64.AppImage
if [ ! -x "$TOOL" ]; then
  mkdir -p build
  curl -fsSL -o "$TOOL" "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"
  chmod +x "$TOOL"
fi
APPIMAGE=dist/ElectriDrive-x86_64.AppImage
if [ "$BUILD_CHANNEL" = "CI-QA" ]; then
  APPIMAGE=dist/ElectriDrive-CI-QA-x86_64.AppImage
fi
ARCH=x86_64 "$TOOL" --appimage-extract-and-run "$APPDIR" "$APPIMAGE"

# ---- .deb ----
ROOT="dist/deb/electridrive_${VERSION}_amd64"
rm -rf "$ROOT"
mkdir -p "$ROOT/opt/electridrive" "$ROOT/usr/bin" "$ROOT/usr/share/applications" \
         "$ROOT/usr/share/icons/hicolor/256x256/apps" "$ROOT/DEBIAN"
cp -r dist/electridrive/* "$ROOT/opt/electridrive/"
ln -sf /opt/electridrive/electridrive "$ROOT/usr/bin/electridrive"
cp assets/electridrive.png "$ROOT/usr/share/icons/hicolor/256x256/apps/electridrive.png"
cp packaging/electridrive.desktop "$ROOT/usr/share/applications/electridrive.desktop"
cat > "$ROOT/DEBIAN/control" <<EOF
Package: electridrive
Version: ${VERSION}
Section: net
Priority: optional
Architecture: amd64
Depends: libfuse2t64 | libfuse2, fuse3 | fuse, libglib2.0-0t64 | libglib2.0-0, libegl1, libgl1, libwayland-cursor0, libwayland-egl1
Maintainer: Pierre Stephan / Electri_C_ity Studios
Description: ElectriDrive ${BUILD_CHANNEL} - Electric-City Drive for Linux
 Beautiful, safety-first Google Drive client: browse, up/download,
 two-way sync and a FUSE files-on-demand mount. Without rclone.
EOF
dpkg-deb --build --root-owner-group "$ROOT"
VERIFY_ARGS=("$ROOT.deb" --expected-version "$VERSION" --max-glibc "$MAX_GLIBC_VERSION")
[ "$BAKED_CLIENT" -eq 1 ] && VERIFY_ARGS+=(--require-baked-client)
"$PY" scripts/verify_deb.py "${VERIFY_ARGS[@]}"
"$PY" scripts/verify_customer_artifact.py "$ROOT.deb" --expected-channel "$BUILD_CHANNEL"
"$PY" scripts/verify_customer_artifact.py "$APPIMAGE" \
  --expected-channel "$BUILD_CHANNEL"

# ---- flat artifacts + checksums (release-ready) ----
cp "$ROOT.deb" "dist/electridrive_${VERSION}_amd64.deb"
( cd dist && sha256sum "$(basename "$APPIMAGE")" "electridrive_${VERSION}_amd64.deb" > SHA256SUMS.txt )

echo "=== customer candidate artifacts (not a release) ==="
ls -la "$APPIMAGE" "dist/electridrive_${VERSION}_amd64.deb" "dist/SHA256SUMS.txt"
echo "--- SHA256SUMS.txt ---"; cat dist/SHA256SUMS.txt
