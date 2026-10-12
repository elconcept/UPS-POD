#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
command -v python3 >/dev/null || { echo 'Zainstaluj Python 3.12 z python.org'; exit 1; }
python3 -m venv .venv-build
. .venv-build/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-desktop.txt
python build/build_app.py
printf '\nGotowe: dist/UPSPOD.app\n'
