"""Process-wide settings — module-level constants.

Per-user settings (provider, api_key, model) live at
``~/.config/voitta-bookmarklet/settings.json`` and are read at
chat-start by ``app.settings``.

The base system prompt is no longer a constant here — it lives in
``plugins/default/system.md`` and is contributed via the plugin loader
(default plugin matches ``host_patterns: ["*"]`` so it always applies).
"""

from __future__ import annotations

import os
from pathlib import Path

# Load a repo-root .env for server deployments (operator-owned secrets:
# Google login creds, public URL, auth secret). python-dotenv ships with
# uvicorn[standard]. No-op on the macOS bundle (no .env shipped) and in dev
# where the file is absent. Existing env vars always win (load_dotenv does
# not override), so the launcher's setdefault()s are unaffected.
try:
    from dotenv import load_dotenv as _load_dotenv

    _load_dotenv(Path(__file__).resolve().parents[2] / ".env")
except Exception:
    pass

HOST = "127.0.0.1"
PORT = 12358

# Plain-HTTP sibling listener. The main port (PORT) serves TLS so the
# bookmarklet works over https on ordinary sites. But on hardened pages
# (e.g. Salesforce Lightning) a strict CSP `connect-src` forbids the page
# from reaching localhost at all, so the widget is loaded + bridged through
# a popup window served from this plain-http port. http://127.0.0.1 is a
# secure context, so the popup needs no trusted cert (zero local admin).
# Serves the SAME ASGI app as PORT — see desktop.py / start.sh.
PLAINTEXT_PORT = 12359

# When running as a packaged .app the launcher sets VOITTA_PROJECT_ROOT to
# ~/Library/Application Support/Voitta Bookmarklet/backend so all derived paths
# (certs, rag, plugins, docs) resolve into the writable user data dir rather
# than the read-only bundle.
_env_root = os.environ.get("VOITTA_PROJECT_ROOT")
PROJECT_ROOT = Path(_env_root) if _env_root else Path(__file__).resolve().parents[1]

# Mutable user data (scripts, python_storage, uploads, conversations DB).
# Resolved like PROJECT_ROOT: VOITTA_DATA_ROOT wins if set (Linux/server
# deployments point it at e.g. /var/lib/voitta), otherwise it defaults to
# the macOS Application Support dir. Never inside the source tree or the
# read-only .app bundle. The macOS launcher sets VOITTA_DATA_ROOT explicitly
# (see src/voitta_bookmarklet/__main__.py) so the default is just a dev fallback.
_env_data = os.environ.get("VOITTA_DATA_ROOT")
USER_DATA_ROOT = (
    Path(_env_data)
    if _env_data
    else Path.home() / "Library" / "Application Support" / "Voitta Bookmarklet" / "backend"
)
TLS_CERT_PATH = PROJECT_ROOT / "certs" / "127.0.0.1+1.pem"
TLS_KEY_PATH = PROJECT_ROOT / "certs" / "127.0.0.1+1-key.pem"

USER_CONFIG_DIR = Path.home() / ".config" / "voitta-bookmarklet"
USER_SETTINGS_PATH = USER_CONFIG_DIR / "settings.json"


def _persisted_auth_secret() -> str:
    """JWT signing secret: VOITTA_AUTH_SECRET if set, else a random value
    persisted at USER_DATA_ROOT/auth_secret so logins survive restarts
    without manual config. Single owner of the secret (Chainlit signs the
    JWT with it; our guard verifies the same JWT)."""
    env = os.environ.get("VOITTA_AUTH_SECRET")
    if env:
        return env
    import secrets

    path = USER_DATA_ROOT / "auth_secret"
    try:
        if path.exists():
            val = path.read_text(encoding="ascii").strip()
            if val:
                return val
        path.parent.mkdir(parents=True, exist_ok=True)
        val = secrets.token_urlsafe(48)
        path.write_text(val, encoding="ascii")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return val
    except Exception:
        return secrets.token_urlsafe(48)


# Server mode = the dedicated Google login client creds are present (.env).
# In that case configure Chainlit's native auth so conversation isolation is
# enforced by Chainlit itself: CHAINLIT_CUSTOM_AUTH flips require_login() on,
# the shared JWT secret signs/verifies the access_token cookie, and
# SameSite=None lets that cookie ride cross-site requests from the bookmarklet.
# These MUST be set before chainlit.auth is imported, so this runs at config
# import (app.main imports app.config first). Desktop/dev sets none of them →
# require_login() stays False → unchanged single-user behaviour.
SERVER_MODE = bool(
    os.environ.get("VOITTA_GOOGLE_AUTH_CLIENT_ID")
    and os.environ.get("VOITTA_GOOGLE_AUTH_CLIENT_SECRET")
)
if SERVER_MODE:
    os.environ.setdefault("CHAINLIT_CUSTOM_AUTH", "true")
    os.environ.setdefault("CHAINLIT_AUTH_SECRET", _persisted_auth_secret())
    os.environ.setdefault("CHAINLIT_COOKIE_SAMESITE", "none")

# Plugins live at the REPO root (sibling of backend/ and frontend/), not
# inside backend/. Both the BE loader (here) and the FE Vite glob read
# from the same tree so plugin authors have a single source of truth.
PLUGINS_DIR = PROJECT_ROOT.parent / "plugins"

# Frontend dist — the compiled React bundle served at /.
# desktop.py sets VOITTA_FRONTEND_DIST to the bundle-internal path before
# uvicorn starts; in dev it falls back to frontend/dist next to backend/.
_env_fe = os.environ.get("VOITTA_FRONTEND_DIST")
FRONTEND_DIST = Path(_env_fe) if _env_fe else PROJECT_ROOT.parent / "frontend" / "dist"

# Project-wide prose docs (indexed by the RAG builder alongside every
# plugin's docs/ tree).
DOCS_DIR = PROJECT_ROOT.parent / "docs"

# Third-party docs pulled in at INSTALL time by a plugin's manifest
# ``docs_repo`` (see installer.sync_plugin_docs). Deliberately a sibling of
# plugins/ rather than a subdir of it: the launcher rmtree's and re-seeds
# plugins/ on every start (src/voitta_bookmarklet/__main__.py), so anything
# cloned inside it would be destroyed and re-fetched each launch. This
# mirrors how lib-sources/ persists across launches.
#   plugin-docs/<plugin>/      converted markdown — indexed into the docs corpus
#   plugin-docs-src/<plugin>/  the upstream git checkout — never indexed
PLUGIN_DOCS_DIR = PROJECT_ROOT.parent / "plugin-docs"
PLUGIN_DOCS_SRC_DIR = PROJECT_ROOT.parent / "plugin-docs-src"

# Built RAG indexes — Chroma persistent store + BM25 + manifest.json.
# Generated by ``scripts/build_rag.py``; gitignored.
RAG_DIR = PROJECT_ROOT.parent / "rag"

# Agent-loop knobs.
# Default cap on assistant tokens per LLM call. User-overridable via
# the Global settings tab (``max_tokens``); the agent loop reads the
# effective value from ``app.settings.load()`` at the start of each turn.
DEFAULT_MAX_TOKENS = 24576
# Default cap on tool-use iterations per user turn. User-overridable
# via the Global settings tab (``max_tool_iterations``); the agent
# loop reads the effective value from ``app.settings.load()``.
DEFAULT_MAX_TOOL_ITERATIONS = 25
