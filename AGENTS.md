# AGENTS.md

Voitta Bookmarklet is an AI assistant that a bookmarklet injects into any web
page. It has a FastAPI + Chainlit backend (`backend/`), a React widget bundled
into one IIFE, `widget.js` (`frontend/`), and host-scoped plugins (`plugins/`).
`README.md` covers user-facing setup. `OPERATIONS.md` is the operator and
data-flow reference; its file paths are the anchors, so re-check its line-level
claims against the code.

## Setup, run, test

- Needs Node 20+ and Python 3.11+. Python 3.12 (the Docker image) and 3.14 are
  known to work. Python 3.15 does not: `onnxruntime`, which `chromadb` requires,
  has no cp315 wheel, so pip stops with `ResolutionImpossible` (checked
  2026-11-13; re-check before relying on it).
- `./build.sh` builds `frontend/dist` and installs the backend into
  `backend/.venv`. It creates the venv only if it is missing, with whichever
  `python3` comes first on PATH. To choose the interpreter, create the venv
  first, for example `python3.14 -m venv backend/.venv`. To switch interpreters,
  delete the venv and start again.
- `./start.sh` serves `https://127.0.0.1:12358` when both
  `backend/certs/127.0.0.1+1.pem` and `backend/certs/127.0.0.1+1-key.pem` exist,
  and plain HTTP otherwise. It also starts a plain-HTTP bridge on `:12359`.
  `./tray.sh` runs the macOS menu-bar app. `./server-start.sh` runs server mode
  without Docker. `./launchd.sh install` runs `./start.sh` as a per-user
  LaunchAgent (starts at login, restarts on exit); run `./launchd.sh uninstall`
  before moving or deleting the checkout.
- Tests: `cd backend && ./.venv/bin/pip install -e '.[dev]' && ./.venv/bin/python -m pytest`
  (`./build.sh` does not install pytest) and `cd frontend && npm test`. CI runs
  neither (`.github/workflows/release.yml` only checks the version bump, tags,
  and builds the image), so run them yourself.

## Testing a browser tool end to end

A tool with `side="hybrid"` runs in the page, so backend tests do not prove it.
This path needs no model and touches no user data:

1. Build the widget: `cd frontend && npm ci && npm run build`.
2. Give a second backend its own home. Put `{"mcpToolsEnabled": true}` in
   `<scratch>/home/.config/voitta-bookmarklet/settings.json`. Then, from
   `backend/`, with `HOME=<scratch>/home VOITTA_DATA_ROOT=<scratch>/data` set
   for both commands:
   - approve the test page's origin:
     `.venv/bin/python -c "from app.services import users; users.approve_site(users.LOCAL_OWNER, 'http://127.0.0.1:12378')"`
   - start it on a spare port:
     `.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 12368`
3. Open the test page in a browser and append
   `<script src="http://127.0.0.1:12368/widget.js">` to it, as the bookmarklet
   does. The page must be plain `http://127.0.0.1`, because this backend serves
   plain HTTP.
4. Call the tool over MCP at `http://127.0.0.1:12368/mcp/`: `vb_sessions`, then
   `vb_call_tool` (`fastmcp.Client` works). The call goes through
   `registry.dispatch` and the real socket.

In a worktree, which has no venv, run the main checkout's
`backend/.venv/bin/python` with `PYTHONPATH=<worktree>/backend`.

To test against a Content-Security-Policy, serve the test page with that
header. A probe run through the Chrome extension's JavaScript tool is exempt
from the page's eval ban until its first `await` (seen 2026-11-13), so check
the policy with an `eval` placed after an `await`.

## Layout

- `backend/app/`: FastAPI app (`main.py`), agent loop (`agent.py`), tools
  (`tools/`), routes, services. Tests are in `backend/tests/`.
- `frontend/src/`: the widget.
- `plugins/<name>/`: `manifest.json` plus optional `backend/`, `frontend/`,
  `docs/` and `prompt.md`. Plugin frontends are bundled into `widget.js`.
- `src/voitta_bookmarklet/`: the macOS `.app` launcher. Its briefcase config is
  the repo-root `pyproject.toml`.
- `backend/app/bookmarklets.py` is the only place the bookmarklet JS is written.
  The tray menu and the `/bookmarklets` page both call it.

## Docs

- `docs/` and `plugins/<name>/docs/` are the master copies. Never edit
  `src/voitta_bookmarklet/resources/{docs,frontend_dist,plugins,vendor_js}`:
  `build_app.sh` generates them and git ignores them.
- After editing anything under `docs/` or `plugins/*/docs/`, rebuild the index
  the in-app agent searches:
  `backend/.venv/bin/python scripts/build_rag.py --corpus docs`.

## Release

- A PR that touches any file outside `**.md`, `docs/**` and `backend/tests/**`
  must raise the version, or the `version-bumped` check fails. A release commit
  (`release X.Y.Z`) changes exactly two files: `version` under `[tool.briefcase]`
  in the repo-root `pyproject.toml`, and `__version__` in
  `src/voitta_bookmarklet/_version.py`.
- A push to `master` with a new version tags `vX.Y.Z`, creates the GitHub
  release, and pushes `ghcr.io/voitta-ai/voitta-bookmarklet:X.Y.Z` and `:latest`
  (linux/amd64 and linux/arm64).
- The default branch is `master`. Commit subjects use prefixes such as `feat:`,
  `fix:` and `deploy:`.

## What a fresh clone lacks

- Git-ignored and per machine: `backend/.venv`, `frontend/node_modules`,
  `frontend/dist`, `backend/certs/`, `.env` and `rag/`. For the certificate pair,
  install mkcert and run `mkcert -install` once per machine. After that, only
  the leaf pair needs recreating:
  `mkdir -p backend/certs && (cd backend/certs && mkcert -cert-file 127.0.0.1+1.pem -key-file 127.0.0.1+1-key.pem 127.0.0.1 localhost)`.
- User data lives outside the repo and survives a re-clone:
  `~/.config/voitta-bookmarklet/settings.json` and
  `~/Library/Application Support/Voitta Bookmarklet/backend/` (`users.sqlite`
  holds accounts and approved sites).
- The first `./start.sh`, `./tray.sh` or `./server-start.sh` runs
  `python -m app.brand_migration`. It renames the old `voitta-compute` config
  dir and `Voitta Compute` data dir into place, and moves an existing
  `voitta-bookmarklet` config dir aside to `*.legacy`. It exits while the old
  Voitta Compute app is running.

## Invariants

- The single-user install has no login. Keep it bound to `127.0.0.1`; the Docker
  quick start publishes `127.0.0.1:12358` and `127.0.0.1:12359` for the same
  reason.
- `/mcp` is loopback-only and refuses requests that carry a browser `Origin`.
- The widget runs only on sites the user approved (`users.approve_site`).
- No secrets in git. `.env.example` is the template; real values live in `.env`
  or the data directory.
