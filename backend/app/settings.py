"""Typed view over the raw settings blob in :mod:`app.services.user_settings`.

The settings file lives at ``~/.config/voitta-compute/settings.json``
and stores everything in one nested object:

    {
      "provider": "anthropic",
      "api_keys": {"anthropic": "sk-..."},
      "models":   {"anthropic": "claude-sonnet-4-5-20250929"},
      "layout":   "chat-right",
      "theme":    "auto",
      "googleOAuth": { "defaultAccount": "acc_...", "accounts": { "acc_...": {
          "label": "...", "clientId": "...", "clientSecret": "...", "tokens": {...} } } },
      "plugins": { "voitta-enterprise": { "mcp": { "url": "...", "api_key": "..." } } }
    }

Core chat code (provider, key, model) goes through this module's
:func:`load` / :func:`save` so the typed defaults are applied. Plugin
code and the Google OAuth service go straight through
``user_settings.read()/write()`` and treat the blob as a free-form
dotted-key namespace.

API keys are write-only on the wire: :func:`redacted_for_wire` produces
the shape the Settings UI consumes, which replaces every plaintext key
with a ``has_api_keys[provider] == bool`` flag.
"""

from __future__ import annotations

import logging
from typing import Any, TypedDict

from app.config import DEFAULT_MAX_TOKENS, DEFAULT_MAX_TOOL_ITERATIONS
from app.services import user_settings

logger = logging.getLogger(__name__)


class UserSettings(TypedDict, total=False):
    provider: str
    api_keys: dict[str, str]
    models: dict[str, str]
    layout: str  # "chat-right" | "chat-left"
    theme: str   # "light" | "dark" | "auto"
    max_tool_iterations: int
    max_tokens: int


# The ``models`` map stores only the user's *pinned* selection per provider.
# It is intentionally empty by default: the list of choices and the default
# model both come from app.services.models_catalog (live cache → bundled
# snapshot), so no model-id string is hardcoded here. An unset provider means
# "use the catalog default", resolved at call time by default_model_for().
_DEFAULT_MODELS: dict[str, str] = {}

_DEFAULTS: UserSettings = {
    "provider": "anthropic",
    "api_keys": {},
    "models": dict(_DEFAULT_MODELS),
    "layout": "chat-right",
    "theme": "auto",
    "max_tool_iterations": DEFAULT_MAX_TOOL_ITERATIONS,
    "max_tokens": DEFAULT_MAX_TOKENS,
}


def _typed_view(blob: dict[str, Any]) -> UserSettings:
    """Project the raw blob into the typed subset, applying defaults."""
    out: UserSettings = {
        "provider": _DEFAULTS["provider"],
        "api_keys": dict(_DEFAULTS["api_keys"]),
        "models": dict(_DEFAULTS["models"]),
        "layout": _DEFAULTS["layout"],
        "theme": _DEFAULTS["theme"],
        "max_tool_iterations": _DEFAULTS["max_tool_iterations"],
        "max_tokens": _DEFAULTS["max_tokens"],
    }
    # One-shot migration from the phase-1 flat schema.
    if "api_key" in blob and "api_keys" not in blob:
        prov = blob.get("provider", "anthropic")
        blob = dict(blob)
        blob["api_keys"] = {prov: blob.pop("api_key")}
    if "model" in blob and "models" not in blob:
        prov = blob.get("provider", "anthropic")
        blob = dict(blob)
        blob["models"] = {prov: blob.pop("model")}

    for k in ("provider", "layout", "theme"):
        if isinstance(blob.get(k), str):
            out[k] = blob[k]  # type: ignore[literal-required]
    if isinstance(blob.get("api_keys"), dict):
        out["api_keys"] = {
            str(p): str(v) for p, v in blob["api_keys"].items() if isinstance(v, str)
        }
    if isinstance(blob.get("models"), dict):
        merged = dict(_DEFAULT_MODELS)
        merged.update({
            str(p): str(v) for p, v in blob["models"].items() if isinstance(v, str)
        })
        out["models"] = merged
    raw_iters = blob.get("max_tool_iterations")
    if isinstance(raw_iters, int) and raw_iters > 0:
        out["max_tool_iterations"] = raw_iters
    raw_tokens = blob.get("max_tokens")
    if isinstance(raw_tokens, int) and raw_tokens > 0:
        out["max_tokens"] = raw_tokens
    return out


def load() -> UserSettings:
    """Read the raw blob and return the typed projection."""
    try:
        blob = user_settings.read()
    except Exception:
        logger.exception("failed to read settings")
        blob = {}
    return _typed_view(blob)


def raw_blob() -> dict[str, Any]:
    """Untyped escape hatch for callers that want to walk the full
    nested settings — plugin code, Google OAuth, MCP url/token lookup."""
    try:
        return user_settings.read()
    except Exception:
        logger.exception("failed to read settings")
        return {}


def save(patch: dict[str, Any]) -> UserSettings:
    """Merge a typed patch into on-disk settings.

    Dict fields (``api_keys``, ``models``) merge per-key rather than
    replacing the whole dict — so PUT-ing ``{"api_keys": {"openai": "..."}}``
    keeps the anthropic key. Pass an explicit ``""`` to clear a single
    entry.
    """
    current = user_settings.read()

    if isinstance(patch.get("provider"), str):
        current["provider"] = patch["provider"]
    if isinstance(patch.get("layout"), str):
        current["layout"] = patch["layout"]
    if isinstance(patch.get("theme"), str):
        current["theme"] = patch["theme"]
    if isinstance(patch.get("max_tool_iterations"), int) and patch["max_tool_iterations"] > 0:
        current["max_tool_iterations"] = patch["max_tool_iterations"]
    if isinstance(patch.get("max_tokens"), int) and patch["max_tokens"] > 0:
        current["max_tokens"] = patch["max_tokens"]

    if isinstance(patch.get("api_keys"), dict):
        keys = dict(current.get("api_keys") or {})
        changed_providers: list[str] = []
        for p, v in patch["api_keys"].items():
            if not isinstance(v, str):
                continue
            if v == "":
                if keys.pop(p, None) is not None:
                    changed_providers.append(p)  # cleared
            else:
                if keys.get(p) != v:
                    changed_providers.append(p)  # set or replaced
                keys[p] = v
        current["api_keys"] = keys
        # Sync moments #1 (key saved) / #7 (key removed): drop the cached
        # model list so the next /api/models fetch reflects the new credential
        # (or reverts to the snapshot when a key was cleared).
        if changed_providers:
            try:
                from app.services import models_catalog

                for p in changed_providers:
                    models_catalog.invalidate(p)
            except Exception:
                logger.exception("model-catalog invalidate on key change failed")

    if isinstance(patch.get("models"), dict):
        models = dict(current.get("models") or {})
        for p, v in patch["models"].items():
            if isinstance(v, str) and v:
                models[p] = v
        current["models"] = models

    user_settings.write(current)
    return _typed_view(current)


def save_dotted(patches: dict[str, Any]) -> dict[str, Any]:
    """Apply a batch of ``"a.b.c": value`` patches to the raw blob and
    persist. Used by the plugin-settings schema renderer, which writes
    fields like ``plugins.voitta-enterprise.mcp.url``.

    Empty-string values DELETE the key (consistent with how the typed
    save() handles api_keys). ``None`` values also delete. Other values
    are written verbatim.
    """
    current = user_settings.read()
    for path, value in patches.items():
        if not isinstance(path, str) or not path:
            continue
        if value is None or value == "":
            _dotted_delete(current, path)
        else:
            _dotted_set(current, path, value)
    user_settings.write(current)
    return current


def _dotted_set(blob: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    cur: dict[str, Any] = blob
    for k in parts[:-1]:
        nxt = cur.get(k)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[k] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _dotted_delete(blob: dict[str, Any], path: str) -> None:
    parts = path.split(".")
    cur: dict[str, Any] | None = blob
    for k in parts[:-1]:
        if not isinstance(cur, dict):
            return
        cur = cur.get(k)
    if isinstance(cur, dict):
        cur.pop(parts[-1], None)


def redacted_for_wire(s: UserSettings | None = None) -> dict[str, Any]:
    """Public-safe shape for the Settings UI.

    Includes the typed fields plus the nested ``plugins`` and
    ``googleOAuth`` slices so plugin panels can prefill — but never
    plaintext LLM api_keys (replaced by ``has_api_keys: {p: bool}``).
    Every per-account ``tokens`` blob under ``googleOAuth.accounts`` is
    redacted down to its ``account_email``; the panel reads connection
    status via ``/api/google/status`` separately.
    """
    s = s if s is not None else load()
    blob = raw_blob()
    keys = s.get("api_keys") or {}
    g = _redact_google_oauth(blob.get("googleOAuth"))
    return {
        "provider": s.get("provider", "anthropic"),
        "models": dict(s.get("models") or _DEFAULT_MODELS),
        "layout": s.get("layout", "chat-right"),
        "theme": s.get("theme", "auto"),
        "max_tool_iterations": s.get("max_tool_iterations", DEFAULT_MAX_TOOL_ITERATIONS),
        "max_tokens": s.get("max_tokens", DEFAULT_MAX_TOKENS),
        # codex has no saved key: "has key" means the codex CLI is signed in.
        "has_api_keys": {**{p: bool(v) for p, v in keys.items()}, "codex": _codex_signed_in()},
        "googleOAuth": g,
        "plugins": dict(blob.get("plugins") or {}),
        # Claude (subscription) brain: whether the Claude Code engine is
        # installed (gates the selector option) and whether this user has a
        # working-or-stored token (drives the "connected" hint). Token status
        # is per-user — resolves under the current-user contextvar.
        "agent_sdk": _agent_sdk_status(),
    }


def _redact_google_oauth(raw: Any) -> dict[str, Any]:
    """Strip token material from the googleOAuth blob (multi-account
    shape; a not-yet-migrated legacy flat blob is handled too), keeping
    each account's ``account_email`` so panels can display identity."""
    g = dict(raw or {})
    # Legacy flat shape — a plain top-level tokens blob.
    legacy_tok = g.pop("tokens", None)
    if isinstance(legacy_tok, dict) and legacy_tok.get("account_email"):
        g["account_email"] = legacy_tok["account_email"]
    accounts = g.get("accounts")
    if isinstance(accounts, dict):
        redacted_accounts: dict[str, Any] = {}
        for aid, acct in accounts.items():
            a = dict(acct or {})
            tok = a.pop("tokens", None)
            if isinstance(tok, dict) and tok.get("account_email"):
                a["account_email"] = tok["account_email"]
            redacted_accounts[aid] = a
        g["accounts"] = redacted_accounts
    return g


def _codex_signed_in() -> bool:
    from app.services.llm.codex import access_token

    retval = access_token() is not None
    return retval


def _agent_sdk_status() -> dict[str, Any]:
    try:
        from app.services.agent_sdk import is_available
        from app.services.agent_sdk.credentials import has_token

        return {"available": is_available(), "has_token": has_token()}
    except Exception:
        logger.exception("agent_sdk status probe failed")
        return {"available": False, "has_token": False}


def api_key_for(provider: str) -> str | None:
    """Look up the saved API key for a single provider, or None."""
    return (load().get("api_keys") or {}).get(provider) or None
