#!/usr/bin/env bash
# Restart a previously sealed QA profile, with isolated OAuth and settings.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
if [[ $# -lt 1 || "$1" != /* ]]; then
    echo "Usage: $0 /absolute/private/qa-profile [GUI arguments]" >&2
    exit 2
fi
qa_profile="$1"
shift
PY="${PY:-$PWD/.venv/bin/python}"
export ELECTRIDRIVE_QA_PROFILE="$qa_profile"
export PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring PYTHONNOUSERSITE=1
exec "$PY" app.py --qa-profile "$qa_profile" "$@"
