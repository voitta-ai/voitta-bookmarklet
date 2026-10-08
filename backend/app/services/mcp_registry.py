"""Agent tools for external agents — the tool registry over ``/mcp``.

The in-app agent works through the tool registry (``app.tools.registry``).
This module exposes that same registry on the app's MCP endpoint, so an
external agent (Claude Code, say) can do everything the in-app agent can
— with the app holding no API key. The ``vb_*`` tools here are a thin
door onto it:

  * ``vb_sessions``       — open pages (bookmarklet tabs) a call can target
  * ``vb_instructions``   — the system prompt the in-app agent gets there
  * ``vb_list_tools``     — the tools available on that page
  * ``vb_describe_tools`` — full descriptions + input schemas
  * ``vb_call_tool``      — validate and run one tool

Calls go through ``registry.dispatch`` exactly like the in-app agent's. A
tool that works in the page (``side="hybrid"``) needs the tab's Chainlit
context, which an MCP request lacks; ``vb_call_tool`` binds the target
session's context first — the same rebind ``ask_user_question`` does.

Differences from the in-app harness: ``ask_user_question`` is not offered
(it asks in the chat pane; an external agent asks its own user), and tool
calls don't appear in the chat pane. Gated by its own switch,
``mcpToolsEnabled`` — wider than debugging: it runs Python and writes to
connected services.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.tools.tool import ToolResult
from jsonschema import ValidationError
from jsonschema import validate as validate_arguments
from mcp import types as mt

from app.tools.registry import ToolCtx, ToolSpec, registry

_log = logging.getLogger(__name__)

# Tools that only make sense inside the in-app chat.
_EXCLUDED = frozenset({"ask_user_question"})

# Same default per-tool ceiling as the in-app agent's bridge.
_TOOL_TIMEOUT_S = 150.0

FAMILY_PREFIX = "vb_"

AGENT_TOOLS_TAG = "[Agent tools]"


class _Target:
    """The page a call runs against: its session (None = no page), host
    and signed-in user."""

    def __init__(self, session_id: str | None, host: str | None,
                 email: str | None, ws: Any) -> None:
        self.session_id, self.host, self.email, self.ws = session_id, host, email, ws


def _live_sessions() -> list[dict[str, Any]]:
    from app.services import cl_sessions

    active = cl_sessions.get_active_session()
    rows = []
    for rec in cl_sessions.snapshot():
        if not rec.get("connected"):
            continue
        rows.append({
            "session_id": rec["session_id"],
            "host": rec.get("host"),
            "url": rec.get("url"),
            "title": rec.get("title"),
            "active": bool(active and active.session_id == rec["session_id"]),
            "last_seen": rec.get("last_seen"),
        })
    return rows


def _resolve(session_id: str | None) -> _Target | dict[str, Any]:
    """The target page, or an error envelope listing what's available.

    No ``session_id``: the most recently focused live tab; with no tab open,
    a page-less target (server-side tools only)."""
    from chainlit.session import WebsocketSession

    from app.services import cl_sessions

    if not session_id:
        active = cl_sessions.get_active_session()
        if active is None:
            return _Target(None, None, None, None)
        session_id = active.session_id
    ws = WebsocketSession.get_by_id(session_id)
    if ws is None:
        return {"ok": False, "error": "no_session",
                "message": f"no open page with session_id {session_id!r} — "
                           "see vb_sessions (the tab may have been closed or reloaded)",
                "sessions": _live_sessions()}
    rec = cl_sessions.get(session_id)
    host = rec.host if rec else None
    if host is None:
        try:
            from chainlit.user_session import user_sessions

            host = (user_sessions.get(session_id) or {}).get("host")
        except Exception:
            host = None
    user = getattr(ws, "user", None)
    email = getattr(user, "identifier", None) if user else None
    return _Target(session_id, host, email, ws)


def _visible(host: str | None) -> list[ToolSpec]:
    return [s for s in registry.visible_for_host(host) if s.name not in _EXCLUDED]


def _summary(text: str, limit: int = 240) -> str:
    first = text.strip().split("\n\n", 1)[0].replace("\n", " ")
    return first if len(first) <= limit else first[: limit - 1].rstrip() + "…"


def _to_content(value: Any) -> list[mt.ContentBlock]:
    """A tool result as MCP content — text and images, same conversion the
    in-app agent's results go through."""
    from app.tools.content import tool_result_content

    out: list[mt.ContentBlock] = []
    for block in tool_result_content(value):
        if block.get("type") == "image":
            out.append(mt.ImageContent(type="image", data=block["data"],
                                       mimeType=block.get("mimeType") or "image/png"))
        else:
            out.append(mt.TextContent(type="text", text=str(block.get("text", ""))))
    return out


def _err(kind: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": kind, "message": message, **extra}


def _chainlit_context(ws: Any) -> Any:
    """A Chainlit context for the target session (its socket emitter)."""
    from chainlit.context import ChainlitContext

    return ChainlitContext(ws)


async def _dispatch(target: _Target, name: str, args: dict[str, Any],
                    timeout_s: float) -> Any:
    """Run one registry tool as the target page's agent would: inside that
    session's Chainlit context, as its user. Runs in a task with its own
    context copy so nothing leaks into the MCP request handler."""
    from app.services.current_user import set_current_email

    ctx = ToolCtx(session_id=target.session_id, host=target.host, email=target.email)
    run_ctx = contextvars.copy_context()

    def _bind() -> None:
        set_current_email(target.email)
        if target.ws is not None:
            from chainlit.context import context_var

            context_var.set(_chainlit_context(target.ws))

    run_ctx.run(_bind)
    task = asyncio.get_running_loop().create_task(
        registry.dispatch(name, args, ctx), context=run_ctx)
    return await asyncio.wait_for(task, timeout=timeout_s)


# ---- the tools ---------------------------------------------------------------


async def vb_sessions() -> dict[str, Any]:
    rows = _live_sessions()
    return {"ok": True, "count": len(rows), "sessions": rows,
            "note": "Pass a session_id to target a page; omitted, calls use the "
                    "active one (marked active). With no page open, only "
                    "server-side tools are available."}


async def vb_instructions(session_id: str | None = None) -> dict[str, Any]:
    target = _resolve(session_id)
    if isinstance(target, dict):
        return target
    from app.services.current_user import set_current_email
    from app.services.system_prompt import compose

    set_current_email(target.email)
    prompt = await asyncio.to_thread(compose, target.host)
    preface = (
        "These are the instructions the in-app Voitta agent works under on "
        f"this page (host {target.host or 'none'}). Follow them when driving "
        "the vb_* tools. Differences for you: ask your own user instead of "
        "ask_user_question (not available here), and your tool calls don't "
        "show in the page's chat pane."
    )
    return {"ok": True, "session_id": target.session_id, "host": target.host,
            "instructions": f"{preface}\n\n{prompt}"}


async def vb_list_tools(session_id: str | None = None) -> dict[str, Any]:
    target = _resolve(session_id)
    if isinstance(target, dict):
        return target
    tools = [{"name": s.name, "summary": _summary(s.description),
              "needs_page": s.side == "hybrid",
              **({"plugin": s.plugin_name} if s.plugin_name else {})}
             for s in sorted(_visible(target.host), key=lambda s: s.name)]
    return {"ok": True, "session_id": target.session_id, "host": target.host,
            "count": len(tools), "tools": tools,
            "next": "vb_describe_tools(names=[...]) for full descriptions and "
                    "input schemas, then vb_call_tool(name, arguments)."}


async def vb_describe_tools(names: list[str], session_id: str | None = None) -> dict[str, Any]:
    target = _resolve(session_id)
    if isinstance(target, dict):
        return target
    visible = {s.name: s for s in _visible(target.host)}
    out, missing = [], []
    for n in names:
        s = visible.get(n)
        if s is None:
            missing.append(n)
            continue
        out.append({"name": s.name, "description": registry._describe(s),
                    "input_schema": s.input_schema, "needs_page": s.side == "hybrid"})
    res: dict[str, Any] = {"ok": True, "host": target.host, "tools": out}
    if missing:
        res["not_available"] = missing
        res["hint"] = "not offered on this page — check vb_list_tools for this session"
    return res


async def vb_call_tool(name: str, arguments: dict[str, Any] | None = None,
                       session_id: str | None = None) -> ToolResult:
    # FastMCP marks a result as an error only when the tool raises; the
    # envelope travels as the error text (JSON, like every other result).
    def fail(kind: str, message: str, **extra: Any) -> ToolResult:
        raise ToolError(json.dumps(_err(kind, message, **extra), ensure_ascii=False, default=str))

    target = _resolve(session_id)
    if isinstance(target, dict):
        raise ToolError(json.dumps(target, ensure_ascii=False, default=str))
    spec = {s.name: s for s in _visible(target.host)}.get(name)
    if spec is None:
        why = ("asks the user in the in-app chat — ask your own user instead"
               if name in _EXCLUDED else
               f"not available on this page (host {target.host or 'none'})")
        return fail("unknown_tool", f"{name!r}: {why}. See vb_list_tools.")
    args = dict(arguments or {})
    try:
        validate_arguments(args, spec.input_schema)
    except ValidationError as exc:
        path = "/".join(str(p) for p in exc.absolute_path)
        return fail("invalid_arguments", f"{exc.message}" + (f" (at {path})" if path else ""))
    if spec.side == "hybrid" and target.ws is None:
        return fail("no_page", f"{name!r} works in the user's page and no page is open — "
                    "ask the user to open the site with the Voitta bookmarklet, then "
                    "check vb_sessions.")

    limit = float(spec.timeout_s or _TOOL_TIMEOUT_S)
    _log.info("vb_call_tool %s session=%s host=%s", name, target.session_id, target.host)
    try:
        res = await _dispatch(target, name, args, limit)
    except (TimeoutError, asyncio.TimeoutError):
        return fail("timeout", f"{name!r} exceeded {int(limit)}s and was aborted — "
                    "try a smaller request")
    except Exception as exc:  # noqa: BLE001 — surface, don't crash the session
        _log.exception("vb_call_tool %s raised", name)
        return fail("tool_crashed", f"{type(exc).__name__}: {exc}")
    if res.ok:
        # Content blocks as-is, so images arrive as images.
        return ToolResult(content=_to_content(res.result))
    raise ToolError(json.dumps(res.error or _err("error", "tool failed"),
                               ensure_ascii=False, default=str))


def register(mcp: Any) -> None:
    """Add the agent-tools family to the FastMCP server."""
    tag = AGENT_TOOLS_TAG

    mcp.tool(name="vb_sessions", description=(
        f"{tag} The pages (bookmarklet tabs) agent tools can act on: session_id, "
        "host, url, title, and which is active. Start here."))(vb_sessions)
    mcp.tool(name="vb_instructions", description=(
        f"{tag} The system prompt the in-app Voitta agent works under on a page "
        "(plugin rules, the active project). Read it before using the other "
        "vb_* tools on that page — the tools assume it. session_id optional "
        "(defaults to the active page)."))(vb_instructions)
    mcp.tool(name="vb_list_tools", description=(
        f"{tag} The Voitta tools available on a page — the same set the in-app "
        "agent gets there (they depend on the site: LinkedIn, eBay, Google, …). "
        "Name, one-line summary, and needs_page. session_id optional."))(vb_list_tools)
    mcp.tool(name="vb_describe_tools", description=(
        f"{tag} Full description and JSON input schema for the named tools. "
        "Read a tool's description before its first call — they carry "
        "contracts the schema doesn't."))(vb_describe_tools)
    mcp.tool(name="vb_call_tool", description=(
        f"{tag} Run one Voitta tool exactly as the in-app agent would, with its "
        "result (text and images). arguments must match the tool's input "
        "schema (vb_describe_tools). Page tools run in the user's open tab; "
        "server tools run in the app."))(vb_call_tool)
