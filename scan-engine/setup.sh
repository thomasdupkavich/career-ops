#!/bin/sh
# Idempotent bootstrap for the JobSpy scan engine.
# Creates a local venv and installs python-jobspy. Safe to re-run.
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "Creating venv (scan-engine/.venv)…"
  python3 -m venv .venv
fi

echo "Installing dependencies…"
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements.txt

echo "✓ scan-engine ready. Test: ./.venv/bin/python jobspy_search.py | head -c 200"
