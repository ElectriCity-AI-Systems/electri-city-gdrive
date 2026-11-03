#!/usr/bin/env bash
# Build both customer candidates from one exact committed source snapshot.
set -euo pipefail
cd "$(dirname "$0")/.."
IMAGE="${ELECTRIDRIVE_RELEASE_IMAGE:-electridrive-release:ubuntu-22.04}"
CLIENT_FILE="${ELECTRIDRIVE_APP_CLIENT_FILE:?Set the explicit approved Desktop OAuth JSON path}"
if [ ! -f "$CLIENT_FILE" ] || [ -L "$CLIENT_FILE" ]; then
  echo 'An explicit regular publisher client file is required.' >&2
  exit 1
fi
CLIENT_FILE="$(realpath "$CLIENT_FILE")"
SNAPSHOT="$(mktemp)"
trap 'rm -f "$SNAPSHOT"' EXIT
python3 scripts/prepare_release_draft.py --snapshot "$SNAPSHOT"
EPOCH="$(git log -1 --format=%ct)"
docker build --file packaging/Dockerfile.release --tag "$IMAGE" .
docker run --rm --user "$(id -u):$(id -g)" \
  --env PY=/opt/electridrive-release/bin/python \
  --env PYTHONDONTWRITEBYTECODE=1 --env PYTHONHASHSEED=0 \
  --env PYINSTALLER_CONFIG_DIR=/tmp/electridrive-build-pyinstaller \
  --env XDG_CACHE_HOME=/tmp/electridrive-build-cache \
  --env XDG_CONFIG_HOME=/tmp/electridrive-build-config \
  --env XDG_STATE_HOME=/tmp/electridrive-build-state \
  --env MAX_GLIBC_VERSION=2.35 --env SOURCE_DATE_EPOCH="$EPOCH" \
  --env ELECTRIDRIVE_BUILD_CHANNEL=CUSTOMER-CANDIDATE \
  --env ELECTRIDRIVE_APP_CLIENT_FILE=/run/electridrive-app-client.json \
  --mount "type=bind,source=$CLIENT_FILE,target=/run/electridrive-app-client.json,readonly" \
  --mount "type=bind,source=$PWD,target=/src" \
  "$IMAGE" bash scripts/build_release.sh
python3 scripts/prepare_release_draft.py --record dist/candidate-manifest.json --source-record "$SNAPSHOT"
