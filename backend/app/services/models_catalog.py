"""Dynamic model-catalog sync with a cache-first, snapshot-fallback policy.

This is the single source of truth for "what models can this provider run".
It replaces the hardcoded model lists that used to live in five places
(frontend dropdown const, frontend settings defaults, ``settings.py``,
``llm/__init__.py``, ``agent_sdk/config.py``).

Policy — a network fetch happens **only** when::

    has_credential and (cache_missing or cache_stale or force)

Otherwise we serve, in order: fresh in-memory/on-disk cache → last-known
cache (even if stale, when a fetch fails) → bundled snapshot. The dropdown
is therefore *never empty* and never blocks on a live call it doesn't need.

Cache layout (``USER_DATA_ROOT/models_cache.json``), per provider::

    {"anthropic": {"cred_fp": "1a2b3c4d", "models": [...],
                   "default": "...", "fetched_at": 1723800000.0}}

``cred_fp`` is a short hash of the credential so a *changed* key forces a
refetch without ever persisting the key itself.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from app.config import USER_DATA_ROOT

logger = logging.getLogger(__name__)

# Providers that fetch a live catalog from an API key.
_API_PROVIDERS = ("anthropic", "openai", "gemini", "requesty", "codex")
# The subscription brain — probed separately, snapshot-only today.
_CLAUDE_CODE = "claude_code"
_ALL_PROVIDERS = (*_API_PROVIDERS, _CLAUDE_CODE)

# How long a live-fetched catalog stays fresh before we revalidate.
CATALOG_TTL_S = 12 * 60 * 60  # 12h

_SNAPSHOT_PATH = Path(__file__).resolve().parent.parent / "data" / "model_catalog_snapshot.json"
_CACHE_PATH = Path(USER_DATA_ROOT) / "models_cache.json"

# In-memory mirror of the on-disk cache. Guarded by _LOCK for the
# read-modify-write of the JSON file (fetches themselves are async and run
# outside the lock).
_LOCK = threading.Lock()
_cache: dict[str, dict[str, Any]] | None = None
_snapshot: dict[str, Any] | None = None


# ---- snapshot -----------------------------------------------------------


def _load_snapshot() -> dict[str, Any]:
    global _snapshot
    if _snapshot is None:
        try:
            _snapshot = json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("models_catalog: failed to read snapshot %s", _SNAPSHOT_PATH)
            _snapshot = {}
    return _snapshot


def _snapshot_for(provider: str) -> dict[str, Any]:
    entry = _load_snapshot().get(provider) or {}
    models = [m for m in (entry.get("models") or []) if isinstance(m, str)]
    default = entry.get("default") if isinstance(entry.get("default"), str) else None
    if not default:
        default = models[0] if models else ""
    return {"models": models, "default": default}


# ---- cache persistence --------------------------------------------------


def _load_cache() -> dict[str, dict[str, Any]]:
    global _cache
    if _cache is None:
        try:
            raw = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
            _cache = raw if isinstance(raw, dict) else {}
        except FileNotFoundError:
            _cache = {}
        except Exception:
            logger.exception("models_catalog: bad cache file, ignoring %s", _CACHE_PATH)
            _cache = {}
    return _cache


def _persist_cache() -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CACHE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_cache or {}, indent=2), encoding="utf-8")
        tmp.replace(_CACHE_PATH)
    except Exception:
        logger.exception("models_catalog: failed to persist cache")


# ---- credentials --------------------------------------------------------


def _credential_for(provider: str) -> str | None:
    """Return the raw credential for a provider, or None if unconfigured."""
    if provider == _CLAUDE_CODE:
        try:
            from app.services.agent_sdk.credentials import load_token

            return load_token()
        except Exception:
            return None
    if provider == "codex":
        from app.services.llm.codex import access_token

        return access_token()
    from app.settings import api_key_for

    return api_key_for(provider)


def _fingerprint(credential: str | None) -> str:
    if not credential:
        return ""
    return hashlib.sha256(credential.encode("utf-8")).hexdigest()[:8]


# ---- live fetch ---------------------------------------------------------


async def _fetch_live(provider: str, credential: str) -> list[str]:
    """Fetch the live model list for a provider. May raise on failure."""
    if provider == _CLAUDE_CODE:
        from app.services.agent_sdk.models import probe_models

        probed = await probe_models()
        if not probed:
            raise RuntimeError("claude_code probe returned nothing (snapshot-only)")
        return probed

    from app.services.llm import get_provider

    adapter = get_provider(provider, credential)  # type: ignore[arg-type]
    return await adapter.list_models()


# ---- public API ---------------------------------------------------------


def _result(models: list[str], default: str | None, source: str, fetched_at: float | None) -> dict[str, Any]:
    if not default or default not in models:
        default = models[0] if models else default or ""
    return {
        "models": models,
        "default": default,
        "source": source,
        "fetched_at": fetched_at,
    }


async def list_models(provider: str, *, force: bool = False) -> dict[str, Any]:
    """Resolve a provider's model catalog under the cache-first policy.

    Returns ``{models, default, source, fetched_at}`` where ``source`` is
    ``"live"`` | ``"cache"`` | ``"snapshot"``. Never raises — a failed live
    fetch degrades to last-known cache, then the bundled snapshot.
    """
    if provider not in _ALL_PROVIDERS:
        return _result([], None, "snapshot", None)

    credential = _credential_for(provider)
    cred_fp = _fingerprint(credential)

    with _LOCK:
        cache = _load_cache()
        entry = cache.get(provider)

    fresh = (
        entry is not None
        and entry.get("cred_fp") == cred_fp
        and cred_fp != ""
        and (time.time() - float(entry.get("fetched_at") or 0)) < CATALOG_TTL_S
    )

    # Serve fresh cache unless a force refresh was requested.
    if entry is not None and fresh and not force:
        return _result(entry.get("models") or [], entry.get("default"), "cache", entry.get("fetched_at"))

    # No credential → we cannot fetch. Serve last-known cache (any age) or snapshot.
    if not credential:
        if entry is not None and entry.get("models"):
            return _result(entry.get("models") or [], entry.get("default"), "cache", entry.get("fetched_at"))
        snap = _snapshot_for(provider)
        return _result(snap["models"], snap["default"], "snapshot", None)

    # Credential present and (missing | stale | forced | changed fp) → fetch live.
    try:
        models = await _fetch_live(provider, credential)
        if not models:
            raise RuntimeError("empty model list")
        default = (entry or {}).get("default")
        snap_default = _snapshot_for(provider)["default"]
        if not default or default not in models:
            default = snap_default if snap_default in models else models[0]
        fetched_at = time.time()
        with _LOCK:
            cache = _load_cache()
            cache[provider] = {
                "cred_fp": cred_fp,
                "models": models,
                "default": default,
                "fetched_at": fetched_at,
            }
            _persist_cache()
        return _result(models, default, "live", fetched_at)
    except Exception:
        logger.warning("models_catalog: live fetch failed for %s; falling back", provider, exc_info=True)
        if entry is not None and entry.get("models"):
            return _result(entry.get("models") or [], entry.get("default"), "cache", entry.get("fetched_at"))
        snap = _snapshot_for(provider)
        return _result(snap["models"], snap["default"], "snapshot", None)


def invalidate(provider: str) -> None:
    """Drop a provider's cached list (on disconnect / key removal)."""
    with _LOCK:
        cache = _load_cache()
        if provider in cache:
            cache.pop(provider, None)
            _persist_cache()
            logger.info("models_catalog: invalidated cache for %s", provider)


def default_model_for(provider: str) -> str:
    """Best-known default model for a provider — cache first, else snapshot.

    Synchronous and side-effect free: reads whatever is already cached and
    otherwise the bundled snapshot. Replaces the scattered ``DEFAULT_MODELS``
    constants. Never triggers a network call.
    """
    with _LOCK:
        entry = _load_cache().get(provider)
    if entry:
        default = entry.get("default")
        models = entry.get("models") or []
        if isinstance(default, str) and default:
            return default
        if models:
            return models[0]
    return _snapshot_for(provider)["default"]


async def warm_cached_credentialed() -> None:
    """Startup warm-up: refetch every provider that has a stored credential.

    Called once at backend startup (sync moment #3). Best-effort — each
    provider is independent and failures fall back per ``list_models``.
    """
    for provider in _ALL_PROVIDERS:
        try:
            if _credential_for(provider):
                await list_models(provider, force=False)
        except Exception:
            logger.warning("models_catalog: warm-up failed for %s", provider, exc_info=True)
