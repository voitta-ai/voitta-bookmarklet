#!/usr/bin/env bash
# Launch the macOS menu-bar tray app.
# rumps owns the main thread; uvicorn runs on a daemon thread.
# Requires ./build.sh to have produced backend/.venv.
set -euo pipefail
cd "$(dirname "$0")/backend"

if [ ! -d .venv ]; then
  echo "[tray.sh] .venv missing — run ./build.sh first" >&2
  exit 1
fi

# Adopt Voitta Compute-era settings/data dirs (one-time; see app/brand_migration.py).
./.venv/bin/python -m app.brand_migration

exec ./.venv/bin/python -m app.desktop
