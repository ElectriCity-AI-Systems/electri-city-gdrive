#!/usr/bin/env bash
# Build the release Debian package on the supported Ubuntu 22.04 / GLIBC 2.35 baseline.
set -euo pipefail
cd "$(dirname "$0")/.."

IMAGE="electridrive-release:ubuntu-22.04"
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(git log -1 --format=%ct)}"
CLIENT_FILE="${ELECTRIDRIVE_APP_CLIENT_FILE:-$HOME/.config/electridrive/app_client.json}"
BUILD_CHANNEL="${ELECTRIDRIVE_BUILD_CHANNEL:-CUSTOMER-CANDIDATE}"

docker build --file packaging/Dockerfile.release --tag "$IMAGE" .

CLIENT_ARGS=()
if [ "$BUILD_CHANNEL" = "CUSTOMER-CANDIDATE" ] && [ -f "$CLIENT_FILE" ]; then
  CLIENT_ARGS+=(
    --mount "type=bind,source=$CLIENT_FILE,target=/run/electridrive-app-client.json,readonly"
    --env ELECTRIDRIVE_APP_CLIENT_FILE=/run/electridrive-app-client.json
  )
elif [ "$BUILD_CHANNEL" != "CI-QA" ] && [ -z "${ELECTRIDRIVE_CLIENT_ID:-}" ]; then
  echo "Customer candidate build requires a publisher-provided OAuth Desktop client." >&2
  exit 1
fi

docker run --rm \
  --user "$(id -u):$(id -g)" \
  --env HOME=/tmp/electridrive-release-home \
  --env MAX_GLIBC_VERSION=2.35 \
  --env "ELECTRIDRIVE_BUILD_CHANNEL=$BUILD_CHANNEL" \
  --env ELECTRIDRIVE_CLIENT_ID \
  --env ELECTRIDRIVE_CLIENT_SECRET \
  --env ELECTRIDRIVE_PICKER_API_KEY \
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

echo "Built package artifacts:"
ls -l dist/electridrive_*_amd64.deb dist/SHA256SUMS.txt
cat dist/SHA256SUMS.txt
