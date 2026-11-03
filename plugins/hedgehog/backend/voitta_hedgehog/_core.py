"""Shared plumbing: the StockTrak call, settings, error envelopes.

Every StockTrak call goes through the ``hedgehog_request`` browser
primitive (plugins/hedgehog/frontend/widget.ts): a same-origin fetch from
the user's own StockTrak tab. The session cookie never reaches this
process; only the parsed response does.

Envelope convention (same as the other plugins):

    {"ok": True, ...payload}
    {"ok": False, "error": kind, "message": str, "hint"?: str}
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.services import user_settings
from app.tools.browser import BrowserToolError, call_browser
from app.tools.registry import ToolCtx

_logger = logging.getLogger(__name__)

PLUGIN = "hedgehog"
ET = ZoneInfo("America/New_York")

HINTS = {
    "session_expired": (
        "The StockTrak session has expired. Ask the user to sign in at app.stocktrak.com "
        "(tick 'Remember me'), then retry once. Do not loop."
    ),
    "wrong_host": "Ask the user to open the bookmarklet on their app.stocktrak.com tab.",
    "server_error_page": (
        "Usually a missing/invalid parameter or nothing to show (e.g. no positions of that type). "
        "Check docs/03-stocktrak-api.md (rag_query) before retrying."
    ),
    "path_not_allowed": "Use one of the documented endpoints (rag_query 'hedgehog stocktrak api').",
}


class StockTrakError(RuntimeError):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def fail(kind: str, message: str, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": False, "error": kind, "message": message}
    if kind in HINTS:
        out["hint"] = HINTS[kind]
    out.update(extra)
    return out


async def st_call(
    ctx: ToolCtx,
    method: str,
    path: str,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
    *,
    write: bool = False,
    keep_html: bool = False,
    timeout_ms: int = 30_000,
) -> dict[str, Any]:
    """One StockTrak request via the browser. Returns the primitive payload
    ({status, kind: json|csv|html, body|rows, ...}); raises StockTrakError."""
    args: dict[str, Any] = {"method": method, "path": path, "params": params or {}, "write": write,
                            "keep_html": keep_html}
    if json_body is not None:
        args["json_body"] = json_body
    try:
        return await call_browser("hedgehog_request", args, ctx, timeout_ms=timeout_ms)
    except BrowserToolError as exc:
        raise StockTrakError(exc.kind, str(exc)) from None


def body_of(res: dict[str, Any]) -> dict[str, Any]:
    """The JSON body of a primitive result, case-folding the Success flag."""
    body = res.get("body") if isinstance(res.get("body"), dict) else {}
    if "success" in body and "Success" not in body:
        body["Success"] = body["success"]
    return body


def rows_of(body: dict[str, Any], key: str = "Html") -> list[dict[str, Any]]:
    """Parsed rows of an HTML-fragment field (see parsers.ts ParsedHtml)."""
    frag = body.get(key)
    return frag.get("rows", []) if isinstance(frag, dict) else []


def text_of(body: dict[str, Any], key: str) -> str:
    frag = body.get(key)
    if isinstance(frag, dict):
        return frag.get("text", "")
    return str(frag or "")


def el_attr(row: dict[str, Any], cls: str, attr: str) -> str | None:
    """data-* attribute of the first element with class ``cls`` in a parsed row."""
    for el in row.get("els") or []:
        if el.get("cls") == cls and attr in (el.get("data") or {}):
            return el["data"][attr]
    return None


def csv_records(rows: list[list[str]]) -> list[dict[str, str]]:
    """Header row + data rows → list of dicts (header keys stripped)."""
    if not rows:
        return []
    head = [h.strip() for h in rows[0]]
    return [dict(zip(head, (c.strip() for c in r))) for r in rows[1:] if any(c.strip() for c in r)]


def today_et() -> str:
    """Today's date in New York as StockTrak's M/D/YYYY."""
    d = datetime.now(ET)
    return f"{d.month}/{d.day}/{d.year}"


def setting(key: str, default: Any = None) -> Any:
    """Read plugins.hedgehog.<key> (canonical, written by the Settings panel)."""
    try:
        blob = user_settings.read()
    except Exception:
        return default
    plugins = blob.get("plugins") if isinstance(blob, dict) else None
    mine = plugins.get(PLUGIN) if isinstance(plugins, dict) else None
    if isinstance(mine, dict) and key in mine:
        return mine[key]
    flat = blob.get(f"plugins.{PLUGIN}.{key}") if isinstance(blob, dict) else None
    return default if flat is None else flat


def trading_enabled() -> bool:
    return bool(setting("trading_enabled", False))


def confirm_orders() -> bool:
    # Unset == ask (the safe default, and what an unchecked box in Settings means).
    return not bool(setting("skip_order_confirmation", False))
