"""Run one Hedgehog tool for an hg key — shared by REST (/v1) and MCP, so they can't drift.

Every call runs with: the key's own tab as the browser (HgTransport), a chat-session id of
``hg:<key id>`` (Hedgehog keeps order previews per session), and the key's settings in place of
the account settings (trading on/off; no human confirmation step — there is no chat to ask in).
Nothing here reads or writes a user's account data.
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from typing import Any

from app.hg import store
from app.hg.link import HgTransport
from app.tools.registry import ToolCtx, registry

# Hedgehog tools offered on hg. Left out: hedgehog_export_csv and hedgehog_session_log write/read
# server-side per-user storage, and hg has no user folders.
EXCLUDED = {"hedgehog_export_csv", "hedgehog_session_log"}

# Read by the Hedgehog plugin (voitta_hedgehog._core.setting / trading.confirm_with_user).
_settings: ContextVar[dict[str, Any] | None] = ContextVar("voitta_hg_settings", default=None)


def plugin_setting(plugin: str, key: str) -> tuple[bool, Any]:
    """``(True, value)`` when an hg call is running and overrides this plugin setting."""
    s = _settings.get()
    if s is None or plugin != "hedgehog" or key not in s:
        return False, None
    return True, s[key]


def active() -> bool:
    return _settings.get() is not None


def tool_names() -> list[str]:
    return sorted(s.name for s in registry.all() if s.name.startswith("hedgehog_") and s.name not in EXCLUDED)


def spec(name: str):
    s = registry.get(name)
    return s if (s is not None and s.name.startswith("hedgehog_") and s.name not in EXCLUDED) else None


async def run(key_row: dict, name: str, args: dict[str, Any], via: str) -> dict[str, Any]:
    """Run tool ``name`` for this key. Returns the tool's own envelope (``ok`` true/false)."""
    s = spec(name)
    if s is None:
        return {"ok": False, "error": "not_found", "message": f"No such tool: {name}"}
    ctx = ToolCtx(session_id=f"hg:{key_row['id']}", host="app.stocktrak.com", browser=HgTransport(key_row["id"]))
    token = _settings.set({"trading_enabled": bool(key_row["trading"]), "skip_order_confirmation": True,
                           "keepalive": bool(key_row["keepalive"])})
    started = time.monotonic()
    try:
        res = await s.handler(dict(args or {}), ctx)
    except Exception as exc:  # a bug in a tool must not take the API down
        res = {"ok": False, "error": "internal_error", "message": f"{type(exc).__name__}: {exc}"[:500]}
    finally:
        _settings.reset(token)
    if not isinstance(res, dict):
        res = {"ok": True, "result": res}
    ok = res.get("ok") is not False
    store.log(key_row["id"], via, name.removeprefix("hedgehog_"), ok, None if ok else str(res.get("error") or ""),
              int((time.monotonic() - started) * 1000))
    return res
