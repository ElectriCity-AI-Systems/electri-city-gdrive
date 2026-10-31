#!/usr/bin/env bash
# Verify immutable upstream appimagetool + type2 runtime assets before use.
# Digests come from the publisher's versioned GitHub release asset metadata.
set -euo pipefail

TOOL_VERSION="1.9.1"
TOOL_SHA256="ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0"
RUNTIME_VERSION="20251108"
RUNTIME_SHA256="2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d"

TOOL="build/appimagetool-x86_64.AppImage"
RUNTIME="build/runtime-x86_64"
mkdir -p build

verified_download() {
  local url="$1" path="$2" expected="$3" executable="$4"
  if [ -e "$path" ] || [ -L "$path" ]; then
    if [ -L "$path" ] || [ ! -f "$path" ]; then
      echo "ERROR: unexpected existing build tool path." >&2
      return 1
    fi
  else
    local temp
    temp="$(mktemp "build/.pinned-appimage-XXXXXXXX")"
    if ! curl --fail --location --silent --show-error --retry 3 \
        --connect-timeout 15 --max-time 120 --output "$temp" "$url"; then
      rm -f "$temp"
      return 1
    fi
    if ! printf '%s  %s\n' "$expected" "$temp" | sha256sum --check --status; then
      rm -f "$temp"
      echo "ERROR: upstream AppImage build asset failed SHA256 verification." >&2
      return 1
    fi
    chmod 600 "$temp"
    mv --no-clobber "$temp" "$path"
  fi
  if ! printf '%s  %s\n' "$expected" "$path" | sha256sum --check --status; then
    echo "ERROR: cached AppImage build asset SHA256 mismatch." >&2
    return 1
  fi
  if [ "$executable" = yes ]; then chmod 700 "$path"; fi
}

verified_download \
  "https://github.com/AppImage/appimagetool/releases/download/${TOOL_VERSION}/appimagetool-x86_64.AppImage" \
  "$TOOL" "$TOOL_SHA256" yes
verified_download \
  "https://github.com/AppImage/type2-runtime/releases/download/${RUNTIME_VERSION}/runtime-x86_64" \
  "$RUNTIME" "$RUNTIME_SHA256" no

echo "Verified pinned appimagetool ${TOOL_VERSION} and runtime ${RUNTIME_VERSION} (SHA256)."
