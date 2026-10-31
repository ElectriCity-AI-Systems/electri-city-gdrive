#!/usr/bin/env bash
# Build a portable ElectriDrive AppImage via PyInstaller + appimagetool.
# Bakes the OAuth client first if ELECTRIDRIVE_CLIENT_ID is set in the environment.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PY:-.venv/bin/python}"
BUILD_CHANNEL="${ELECTRIDRIVE_BUILD_CHANNEL:-CUSTOMER-CANDIDATE}"

"$PY" -c 'import PyInstaller' >/dev/null || {
  echo "PyInstaller is missing; install packaging/requirements-release.txt first." >&2
  exit 1
}
BAKED=electridrive/google_api/client_baked.json
CHANNEL_FILE=electridrive/build_channel.txt
BAKED_CREATED=0
CHANNEL_CREATED=0
cleanup() {
  [ "$BAKED_CREATED" -eq 0 ] || rm -f "$BAKED"
  [ "$CHANNEL_CREATED" -eq 0 ] || rm -f "$CHANNEL_FILE"
}
trap cleanup EXIT
if [ -e "$BAKED" ] || [ -L "$BAKED" ] || [ -e "$CHANNEL_FILE" ] || [ -L "$CHANNEL_FILE" ]; then
  echo "Refusing to build with pre-existing generated OAuth or channel files." >&2
  exit 1
fi
if [ "$BUILD_CHANNEL" = "CI-QA" ]; then
  CHANNEL_CREATED=1
  printf '%s\n' "$BUILD_CHANNEL" > "$CHANNEL_FILE"
elif [ "$BUILD_CHANNEL" = "CUSTOMER-CANDIDATE" ]; then
  if { [ -n "${ELECTRIDRIVE_CLIENT_ID:-}" ] && [ -n "${ELECTRIDRIVE_CLIENT_SECRET:-}" ]; } \
      || { [ -n "${ELECTRIDRIVE_APP_CLIENT_FILE:-}" ] && [ -f "$ELECTRIDRIVE_APP_CLIENT_FILE" ]; }; then
    "$PY" scripts/bake_client.py
    BAKED_CREATED=1
    CHANNEL_CREATED=1
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
RUNTIME=build/runtime-x86_64
bash scripts/get_pinned_appimagetool.sh

APPIMAGE=dist/ElectriDrive-x86_64.AppImage
[ "$BUILD_CHANNEL" = "CI-QA" ] && APPIMAGE=dist/ElectriDrive-CI-QA-x86_64.AppImage
ARCH=x86_64 "$TOOL" --appimage-extract-and-run --runtime-file "$RUNTIME" "$APPDIR" "$APPIMAGE"
"$PY" scripts/verify_customer_artifact.py "$APPIMAGE" --expected-channel "$BUILD_CHANNEL"
echo "Built: $APPIMAGE"
