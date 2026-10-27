#!/usr/bin/env bash
# Build a portable ElectriDrive AppImage via PyInstaller + appimagetool.
# Bakes the OAuth client first if ELECTRIDRIVE_CLIENT_ID is set in the environment.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PY:-.venv/bin/python}"

"$PY" -c 'import PyInstaller' >/dev/null || {
  echo "PyInstaller is missing; install packaging/requirements-release.txt first." >&2
  exit 1
}
# AppImages are customer-distributable, never placeholder QA fixtures.
BAKED=electridrive/google_api/client_baked.json
umask 077
trap 'rm -f "$BAKED"' EXIT
rm -f "$BAKED"
if [ -n "${ELECTRIDRIVE_CLIENT_ID:-}" ]; then
  "$PY" scripts/bake_client.py
elif [ -n "${ELECTRIDRIVE_APP_CLIENT_FILE:-}" ] && [ -f "$ELECTRIDRIVE_APP_CLIENT_FILE" ]; then
  cp "$ELECTRIDRIVE_APP_CLIENT_FILE" "$BAKED"
else
  echo "RELEASE BLOCKED: set ELECTRIDRIVE_APP_CLIENT_FILE (Desktop OAuth + Picker key)." >&2
  exit 1
fi
"$PY" scripts/validate_distribution_oauth.py "$BAKED"
umask 022

rm -rf build dist
"$PY" -m PyInstaller --noconfirm packaging/electridrive.spec
rm -f "$BAKED"   # captured into the build; keep source clean
# Verify that PyInstaller actually copied the real OAuth configuration.
"$PY" - <<'PY'
from pathlib import Path
from scripts.validate_distribution_oauth import validate_client_file
paths = list(Path("dist/electridrive").rglob("client_baked.json"))
if len(paths) != 1:
    raise SystemExit("RELEASE BLOCKED: built AppImage tree lacks a unique OAuth client.")
errors = validate_client_file(paths[0])
if errors:
    raise SystemExit("RELEASE BLOCKED: built AppImage OAuth config is invalid: " +
                     "; ".join(errors))
print("Verified embedded OAuth client (values not displayed).")
PY

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
