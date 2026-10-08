#!/usr/bin/env bash
# One-shot Linux/server bring-up: data path → submodules → build → RAG → serve.
#
# Unlike start.sh (dev/loopback, auto-TLS if certs exist) this is meant for a
# headless server behind a reverse proxy: it forces PLAIN HTTP and binds the
# single app process. Put TLS termination (nginx/caddy/etc.) in front.
#
# Everything is idempotent — re-running skips work that's already done:
#   • submodules: git no-ops if already checked out
#   • build.sh:   npm/pip are incremental
#   • RAG:        skipped when app.rag_build.is_built() is true (use --rebuild-rag to force)
#
# Override anything via env:
#   VOITTA_DATA_ROOT   where mutable data lives   (default: ~/.local/share/voitta)
#   VOITTA_HOST        bind address               (default: 127.0.0.1)
#   VOITTA_PORT        bind port                  (default: 12358)
#
# Flags:
#   --rebuild-rag      force a RAG rebuild even if indexes exist
#   --skip-build       skip submodules + build.sh (just (maybe) RAG + serve)
set -euo pipefail
cd "$(dirname "$0")"

# ---- config (env-overridable) ---------------------------------------------
export VOITTA_DATA_ROOT="${VOITTA_DATA_ROOT:-$HOME/.local/share/voitta}"
HOST="${VOITTA_HOST:-127.0.0.1}"
PORT="${VOITTA_PORT:-12358}"

REBUILD_RAG=0
SKIP_BUILD=0
for arg in "$@"; do
  case "$arg" in
    --rebuild-rag) REBUILD_RAG=1 ;;
    --skip-build)  SKIP_BUILD=1 ;;
    *) echo "[server-start] unknown flag: $arg" >&2; exit 2 ;;
  esac
done

mkdir -p "$VOITTA_DATA_ROOT"
echo "[server-start] VOITTA_DATA_ROOT=$VOITTA_DATA_ROOT"

# ---- 1. submodules + build -------------------------------------------------
if [ "$SKIP_BUILD" = "0" ]; then
  echo "[server-start] submodules (lib-sources/*)"
  git submodule update --init --recursive --depth 1

  echo "[server-start] build.sh (frontend bundle + backend venv)"
  ./build.sh
else
  echo "[server-start] --skip-build: skipping submodules + build.sh"
fi

PY="$(pwd)/backend/.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "[server-start] $PY missing — run without --skip-build first" >&2
  exit 1
fi

# Adopt the Voitta Compute-era settings dir (one-time; see app/brand_migration.py).
( cd backend && "$PY" -m app.brand_migration )

# ---- 1b. NVIDIA GPU acceleration for embeddings ----------------------------
# chromadb pulls CPU `onnxruntime` transitively, so build.sh/the installer
# clobber any onnxruntime-gpu. On a CUDA box we (re)install onnxruntime-gpu
# AFTER build.sh, add the cuDNN9/cuBLAS wheels, and put the wheel-provided
# CUDA libs on LD_LIBRARY_PATH — otherwise onnxruntime "sees" CUDA but fails
# to load libcudnn.so.9 and silently falls back to CPU. The exported
# LD_LIBRARY_PATH carries through to the uvicorn exec so runtime RAG rebuilds
# also hit the GPU. Skipped entirely when no NVIDIA GPU is present (e.g. mac).
if command -v nvidia-smi >/dev/null 2>&1; then
  SITE="$("$PY" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
  if ! "$PY" -c 'import onnxruntime as o,sys; sys.exit(0 if "CUDAExecutionProvider" in o.get_available_providers() else 1)' 2>/dev/null; then
    echo "[server-start] NVIDIA GPU detected — installing onnxruntime-gpu + cuDNN"
    "$PY" -m pip uninstall -y onnxruntime onnxruntime-gpu >/dev/null 2>&1 || true
    "$PY" -m pip install -q onnxruntime-gpu "nvidia-cudnn-cu12>=9,<10" nvidia-cublas-cu12
  else
    echo "[server-start] NVIDIA GPU detected — onnxruntime-gpu already present"
  fi
  for d in "$SITE"/nvidia/*/lib; do
    [ -d "$d" ] && LD_LIBRARY_PATH="$d:${LD_LIBRARY_PATH:-}"
  done
  export LD_LIBRARY_PATH
fi

# ---- 2. RAG indexes (change-detected — same mechanism as the desktop app) -
# build_all() is the desktop installer's entry point: it hashes the docs
# content + checks the lib-sources submodule SHAs against stamp files, skips
# whichever corpus is unchanged, and (re)writes the stamps. Calling it here
# (instead of the raw build_rag.py, which never wrote stamps) gives the server
# the identical "only re-index what changed" behaviour on every start.
# --rebuild-rag clears the stamps first to force a full rebuild.
echo "[server-start] RAG: checking docs/source for changes…"
( cd backend && VB_FORCE_RAG="$REBUILD_RAG" "$PY" - <<'PY'
import os, sys
from app import rag_build

# build_all() redirects sys.stdout/stderr to capture the build script's output
# and feed it to this callback — so the callback must write to the ORIGINAL
# stream (sys.__stdout__), or print() would re-enter the capture infinitely.
_out = sys.__stdout__
def log(line):
    _out.write("  " + line + "\n")
    _out.flush()

if os.environ.get("VB_FORCE_RAG") == "1":
    for p in (rag_build._docs_stamp_path(), rag_build._deployed_stamp_path()):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    log("--rebuild-rag: cleared stamps, forcing full rebuild")

ok = rag_build.build_all(log)
sys.exit(0 if ok else 1)
PY
) || { echo "[server-start] RAG build failed" >&2; exit 1; }

# ---- 3. serve (plain HTTP, single process) --------------------------------
# Mirror start.sh's known-good layout: run from backend/ so chainlit + app.*
# relative paths resolve exactly as they do in dev.
echo "[server-start] http://$HOST:$PORT  (plain HTTP — terminate TLS upstream)"
cd backend
exec ./.venv/bin/uvicorn app.main:app --host "$HOST" --port "$PORT"
