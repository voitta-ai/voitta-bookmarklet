#!/usr/bin/env bash
# Single-process serve: FastAPI + Chainlit + built frontend.
# Run ./build.sh first.
set -euo pipefail
cd "$(dirname "$0")/backend"

HOST="${VOITTA_HOST:-127.0.0.1}"
PORT="${VOITTA_PORT:-12358}"
BRIDGE_PORT="${VOITTA_BRIDGE_PORT:-12359}"
CERT="certs/127.0.0.1+1.pem"
KEY="certs/127.0.0.1+1-key.pem"

if [ ! -d .venv ]; then
  echo "[start.sh] .venv missing — run ./build.sh first" >&2
  exit 1
fi

# Adopt Voitta Compute-era settings/data dirs (one-time; see app/brand_migration.py).
./.venv/bin/python -m app.brand_migration

ARGS=(app.main:app --host "$HOST" --port "$PORT")
if [ "${VOITTA_RELOAD:-0}" = "1" ]; then ARGS+=(--reload); fi

if [ -f "$CERT" ] && [ -f "$KEY" ]; then
  echo "[start.sh] https://$HOST:$PORT"
  ARGS+=(--ssl-certfile "$CERT" --ssl-keyfile "$KEY")
else
  echo "[start.sh] http://$HOST:$PORT  (no TLS cert)"
fi

# Plain-http sibling listener for the hardened-site bridge popup (same app,
# no TLS). Backgrounded; killed when this script exits.
echo "[start.sh] bridge http://$HOST:$BRIDGE_PORT"
./.venv/bin/uvicorn app.main:app --host "$HOST" --port "$BRIDGE_PORT" --log-level warning &
BRIDGE_PID=$!
trap 'kill "$BRIDGE_PID" 2>/dev/null || true' EXIT INT TERM

./.venv/bin/uvicorn "${ARGS[@]}"
