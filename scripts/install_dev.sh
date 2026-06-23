#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
python -m pytest -q
printf '\nElectriDrive dev install complete. Start with: source .venv/bin/activate && python app.py\n'
