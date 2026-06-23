#!/usr/bin/env bash
# Install a desktop launcher + icon for the local (venv) checkout, so ElectriDrive
# shows up in the application menu. Re-run after moving the project.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICON_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/256x256/apps"
mkdir -p "$APP_DIR" "$ICON_DIR"

# Ensure the icon exists (render it if missing).
if [ ! -f "$ROOT/assets/electridrive.png" ]; then
  "$ROOT/.venv/bin/python" "$ROOT/scripts/make_icon.py" || true
fi
cp -f "$ROOT/assets/electridrive.png" "$ICON_DIR/electridrive.png"

PYTHON="$ROOT/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"

cat > "$APP_DIR/electridrive.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=ElectriDrive
GenericName=Google Drive Client
Comment=Browse, sync and mount Google Drive — without rclone
Exec=$PYTHON $ROOT/app.py
Icon=electridrive
Terminal=false
Categories=Network;FileTransfer;Utility;
Keywords=Google;Drive;Cloud;Sync;Backup;Mount;
StartupNotify=true
EOF

chmod +x "$APP_DIR/electridrive.desktop" || true
command -v update-desktop-database >/dev/null 2>&1 && \
  update-desktop-database "$APP_DIR" >/dev/null 2>&1 || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && \
  gtk-update-icon-cache -f "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor" >/dev/null 2>&1 || true

printf 'Installed ElectriDrive launcher to %s\n' "$APP_DIR/electridrive.desktop"
