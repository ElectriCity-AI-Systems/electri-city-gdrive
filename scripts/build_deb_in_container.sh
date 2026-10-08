#!/usr/bin/env bash
# Build the release Debian package on the supported Ubuntu 22.04 / GLIBC 2.35 baseline.
set -euo pipefail
cd "$(dirname "$0")/.."

IMAGE="electridrive-release:ubuntu-22.04"
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(git log -1 --format=%ct)}"
CLIENT_FILE="${ELECTRIDRIVE_APP_CLIENT_FILE:-$HOME/.config/electridrive/app_client.json}"

docker build --file packaging/Dockerfile.release --tag "$IMAGE" .

CLIENT_ARGS=()
if [ -f "$CLIENT_FILE" ]; then
  CLIENT_ARGS+=(
    --mount "type=bind,source=$CLIENT_FILE,target=/run/electridrive-app-client.json,readonly"
    --env ELECTRIDRIVE_APP_CLIENT_FILE=/run/electridrive-app-client.json
  )
elif [ "${ALLOW_PLACEHOLDER_CLIENT:-0}" != "1" ]; then
  echo "No OAuth client configuration found at $CLIENT_FILE." >&2
  echo "Set ELECTRIDRIVE_APP_CLIENT_FILE or ALLOW_PLACEHOLDER_CLIENT=1 for a non-release test build." >&2
  exit 1
fi

docker run --rm \
  --user "$(id -u):$(id -g)" \
  --env HOME=/tmp/electridrive-release-home \
  --env MAX_GLIBC_VERSION=2.35 \
  --env PY=/opt/electridrive-release/bin/python \
  --env PYTHONHASHSEED=0 \
  --env "SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH" \
  --mount "type=bind,source=$PWD,target=/src" \
  "${CLIENT_ARGS[@]}" \
  "$IMAGE" \
  bash -lc '
    set -euo pipefail
    mkdir -p "$HOME"
    bash scripts/build_deb.sh
    version="$($PY -c "from electridrive.config import APP_VERSION; print(APP_VERSION)")"
    cp "dist/deb/electridrive_${version}_amd64.deb" "dist/electridrive_${version}_amd64.deb"
    (cd dist && sha256sum "electridrive_${version}_amd64.deb" > SHA256SUMS.txt)
  '

echo "Release-ready artifacts:"
ls -l dist/electridrive_*_amd64.deb dist/SHA256SUMS.txt
cat dist/SHA256SUMS.txt
