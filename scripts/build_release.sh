#!/usr/bin/env bash
# Build BOTH distributables (AppImage + .deb) from a single PyInstaller run and write
# SHA256SUMS.txt with flat basenames (ready to attach to a GitHub Release).
#
# Bakes the built-in desktop OAuth client from $ELECTRIDRIVE_CLIENT_ID, else from
# ~/.config/electridrive/app_client.json. NEVER bundles tokens, the license signing
# key, or user config — only the intended desktop client (see packaging/electridrive.spec).
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PY:-.venv/bin/python}"
VERSION="$("$PY" -c 'from electridrive.config import APP_VERSION; print(APP_VERSION)')"
MAX_GLIBC_VERSION="${MAX_GLIBC_VERSION:-2.35}"

"$PY" -c 'import PyInstaller' >/dev/null || {
  echo "PyInstaller is missing; install packaging/requirements-release.txt first." >&2
  exit 1
}

BAKED=electridrive/google_api/client_baked.json
BAKED_CLIENT=0
trap 'rm -f "$BAKED"' EXIT
if [ -n "${ELECTRIDRIVE_CLIENT_ID:-}" ]; then
  "$PY" scripts/bake_client.py
  BAKED_CLIENT=1
elif [ -n "${ELECTRIDRIVE_APP_CLIENT_FILE:-}" ] && [ -f "$ELECTRIDRIVE_APP_CLIENT_FILE" ]; then
  cp "$ELECTRIDRIVE_APP_CLIENT_FILE" "$BAKED"
  echo "Baked client from ELECTRIDRIVE_APP_CLIENT_FILE (values not displayed)"
  BAKED_CLIENT=1
elif [ -f "$HOME/.config/electridrive/app_client.json" ]; then
  cp "$HOME/.config/electridrive/app_client.json" "$BAKED"
  echo "Baked client from the user config (values not displayed)"
  BAKED_CLIENT=1
else
  echo "WARN: no client to bake — build ships the placeholder client." >&2
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
ARCH=x86_64 "$TOOL" --appimage-extract-and-run "$APPDIR" "dist/ElectriDrive-x86_64.AppImage"

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
Description: ElectriDrive - Electric-City Drive for Linux
 Beautiful, safety-first Google Drive client: browse, up/download,
 two-way sync and a FUSE files-on-demand mount. Without rclone.
EOF
dpkg-deb --build --root-owner-group "$ROOT"
VERIFY_ARGS=("$ROOT.deb" --expected-version "$VERSION" --max-glibc "$MAX_GLIBC_VERSION")
[ "$BAKED_CLIENT" -eq 1 ] && VERIFY_ARGS+=(--require-baked-client)
"$PY" scripts/verify_deb.py "${VERIFY_ARGS[@]}"

# ---- flat artifacts + checksums (release-ready) ----
cp "$ROOT.deb" "dist/electridrive_${VERSION}_amd64.deb"
( cd dist && sha256sum "ElectriDrive-x86_64.AppImage" "electridrive_${VERSION}_amd64.deb" > SHA256SUMS.txt )

echo "=== release artifacts ==="
ls -la "dist/ElectriDrive-x86_64.AppImage" "dist/electridrive_${VERSION}_amd64.deb" "dist/SHA256SUMS.txt"
echo "--- SHA256SUMS.txt ---"; cat dist/SHA256SUMS.txt
