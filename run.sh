#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# Windows venvs put executables in Scripts/, everything else in bin/.
if [ -d /c/Windows ] || [ -n "${WINDIR:-}" ]; then BIN=Scripts; else BIN=bin; fi

if [ ! -d .venv ]; then
  echo "Creating virtualenv and installing dependencies. This takes a minute."
  python -m venv .venv || python3 -m venv .venv
  "./.venv/$BIN/python" -m pip install --quiet --upgrade pip
  "./.venv/$BIN/python" -m pip install --quiet -r requirements.txt
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from the template. Add your Foundry key, then run this again."
  exit 1
fi

echo "Checking external services before starting."
"./.venv/$BIN/python" scripts/check_apis.py || true

exec "./.venv/$BIN/python" -m uvicorn app.main:app --reload --port 8000
