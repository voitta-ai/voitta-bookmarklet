"""``/mcp`` on hg.voitta.ai: the Hedgehog tools only, as first-class MCP tools.

Each Hedgehog tool appears under its own name with its own input schema (no generic "call a
tool" indirection). Auth is the hg key (``Authorization: Bearer hgk_…``), checked at the ASGI gate
and again on every call; each call runs exactly like the REST API does (app.hg.runner), so it only
ever reaches the key's own StockTrak tab.
"""

from __future__ import annotations

import json
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import Tool
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.hg import runner, store

INSTRUCTIONS = (
    "Hedgehog Camp (StockTrak) trading game, through the user's own StockTrak tab. The tab must be open "
    "with the Hedgehog panel (bookmarklet from hg.voitta.ai): if a tool returns tab_offline, ask the user "
    "to open app.stocktrak.com and click the bookmarklet. Orders need a trade_note (the game requires it). "
    "hedgehog_preview_order then hedgehog_place_order(preview_id) places an order. session_expired means "
    "StockTrak logged the user out: ask them to log in again; never retry an order on your own."
)


def _key_from_request() -> dict | None:
    from fastmcp.server.dependencies import get_http_request

    from app.services.mcp_auth import bearer

    try:
        return store.lookup(bearer(get_http_request().headers))
    except Exception:
        return None


class HedgehogTool(Tool):
    """One Hedgehog registry tool, exposed as an MCP tool for the calling key."""

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        row = _key_from_request()
        if row is None:
            raise ToolError("Hedgehog API key missing or revoked.")
        res = await runner.run(row, self.name, arguments or {}, via="mcp")
        text = json.dumps(res, ensure_ascii=False, default=str)
        if res.get("ok") is False:
            raise ToolError(text)
        return ToolResult(content=[TextContent(type="text", text=text)], structured_content=res)


def build_server() -> FastMCP:
    mcp = FastMCP(name="hedgehog", instructions=INSTRUCTIONS)
    for name in runner.tool_names():
        s = runner.spec(name)
        schema = dict(s.input_schema or {"type": "object", "properties": {}})
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        mcp.add_tool(HedgehogTool(name=name, description=s.description, parameters=schema))
    return mcp


class _Gate:
    """No key, a revoked key, or a browser ``Origin`` → refused before MCP sees the request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        from app.services.mcp_auth import bearer

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        if headers.get("origin"):
            await JSONResponse({"error": {"code": "not_allowed", "message": "MCP is for CLI/desktop clients, not browser pages.",
                                          "retryable": False}}, status_code=403)(scope, receive, send)
            return
        if store.lookup(bearer(headers)) is None:
            await JSONResponse({"error": {"code": "unauthorized", "retryable": False,
                                          "message": "Send 'Authorization: Bearer hgk_…' (your key is in the Hedgehog panel)."}},
                               status_code=401)(scope, receive, send)
            return

        async def send_unbuffered(message) -> None:
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), (b"x-accel-buffering", b"no")]}
            await send(message)

        await self.app(scope, receive, send_unbuffered)


_asgi = None


def asgi():
    """The gated MCP Starlette app (built once; its lifespan must be entered by the main app)."""
    global _asgi
    if _asgi is None:
        inner = build_server().http_app(path="/", stateless_http=True, transport="http")
        inner.add_middleware(_Gate)
        _asgi = inner
    return _asgi
