#!/usr/bin/env bash
# Build a .deb that installs ElectriDrive into /opt (self-contained PyInstaller bundle).
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PY:-.venv/bin/python}"

"$PY" -m pip install --quiet --upgrade pyinstaller
[ -n "${ELECTRIDRIVE_CLIENT_ID:-}" ] && "$PY" scripts/bake_client.py || true

rm -rf build dist
"$PY" -m PyInstaller --noconfirm packaging/electridrive.spec
rm -f electridrive/google_api/client_baked.json   # captured into the build; keep source clean

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
echo "Built: ${ROOT}.deb"
