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

"$PY" -m pip install --quiet --upgrade pyinstaller

BAKED=electridrive/google_api/client_baked.json
if [ -n "${ELECTRIDRIVE_CLIENT_ID:-}" ]; then
  "$PY" scripts/bake_client.py
elif [ -f "$HOME/.config/electridrive/app_client.json" ]; then
  cp "$HOME/.config/electridrive/app_client.json" "$BAKED"
  echo "Baked client from ~/.config/electridrive/app_client.json"
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
Depends: libfuse3-3 | fuse3
Maintainer: Pierre Stephan / Electri_C_ity Studios
Description: ElectriDrive - Electric-City Drive for Linux
 Beautiful, safety-first Google Drive client: browse, up/download,
 two-way sync and a FUSE files-on-demand mount. Without rclone.
EOF
dpkg-deb --build --root-owner-group "$ROOT"

# ---- flat artifacts + checksums (release-ready) ----
cp "$ROOT.deb" "dist/electridrive_${VERSION}_amd64.deb"
( cd dist && sha256sum "ElectriDrive-x86_64.AppImage" "electridrive_${VERSION}_amd64.deb" > SHA256SUMS.txt )

echo "=== release artifacts ==="
ls -la "dist/ElectriDrive-x86_64.AppImage" "dist/electridrive_${VERSION}_amd64.deb" "dist/SHA256SUMS.txt"
echo "--- SHA256SUMS.txt ---"; cat dist/SHA256SUMS.txt
