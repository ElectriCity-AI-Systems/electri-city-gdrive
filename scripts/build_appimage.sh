#!/usr/bin/env bash
# Build a portable ElectriDrive AppImage via PyInstaller + appimagetool.
# Bakes the OAuth client first if ELECTRIDRIVE_CLIENT_ID is set in the environment.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PY:-.venv/bin/python}"

"$PY" -m pip install --quiet --upgrade pyinstaller
[ -n "${ELECTRIDRIVE_CLIENT_ID:-}" ] && "$PY" scripts/bake_client.py || true

rm -rf build dist
"$PY" -m PyInstaller --noconfirm packaging/electridrive.spec
rm -f electridrive/google_api/client_baked.json   # captured into the build; keep source clean

APPDIR=dist/ElectriDrive.AppDir
rm -rf "$APPDIR"; mkdir -p "$APPDIR/usr/bin"
cp -r dist/electridrive/* "$APPDIR/usr/bin/"
cp assets/electridrive.png "$APPDIR/electridrive.png"
cat > "$APPDIR/electridrive.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=ElectriDrive
Exec=electridrive
Icon=electridrive
Categories=Network;FileTransfer;Utility;
Terminal=false
EOF
cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/bash
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/bin/electridrive" "$@"
EOF
chmod +x "$APPDIR/AppRun"

TOOL=build/appimagetool-x86_64.AppImage
if [ ! -x "$TOOL" ]; then
  mkdir -p build
  curl -fsSL -o "$TOOL" \
    "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"
  chmod +x "$TOOL"
fi
# --appimage-extract-and-run avoids needing FUSE to RUN appimagetool itself.
ARCH=x86_64 "$TOOL" --appimage-extract-and-run "$APPDIR" dist/ElectriDrive-x86_64.AppImage
echo "Built: dist/ElectriDrive-x86_64.AppImage"
