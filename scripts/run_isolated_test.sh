#!/usr/bin/env bash
# Start this source checkout with a fresh private QA profile and no saved tokens.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
PY="${PY:-$PWD/.venv/bin/python}"
if [[ ! -x "$PY" ]]; then
    echo 'Create the local environment first: python3 -m venv .venv && .venv/bin/python -m pip install -e ".[dev]"' >&2
    exit 1
fi
profile="$(mktemp -d "${TMPDIR:-/tmp}/electridrive-qa.XXXXXXXX")"
chmod 700 "$profile"
mkdir -m 700 "$profile"/{home,config,state,cache,data}
# Do not inherit OAuth, scope or license overrides from the ordinary session.
for name in $(compgen -e); do
    if [[ "$name" == ELECTRIDRIVE_* ]]; then unset "$name"; fi
done
export HOME="$profile/home"
export XDG_CONFIG_HOME="$profile/config" XDG_STATE_HOME="$profile/state"
export XDG_CACHE_HOME="$profile/cache" XDG_DATA_HOME="$profile/data"
export PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring PYTHONNOUSERSITE=1
printf 'Isolated ElectriDrive QA profile: %s\n' "$profile"
exec "$PY" app.py "$@"
