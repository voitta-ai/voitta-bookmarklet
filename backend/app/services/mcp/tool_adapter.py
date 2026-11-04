"""Synthesise ``ToolSpec`` instances from remote MCP tools + normalise results.

Two concerns live here:

1. **Schema mapping** — a remote ``mcp.types.Tool`` (with ``name``,
   ``description``, ``inputSchema``) becomes a local ``ToolSpec`` with
   the same surface and a generated async handler. The synthetic tool
   name carries the plugin's ``tool_prefix`` so multiple plugins can't
   collide and so the LLM can tell a remote tool from a local one at
   a glance (e.g. ``vre_search`` vs ``rag_query``).

2. **Result mapping** — fastmcp's ``CallToolResult`` carries a list of
   content blocks (text, image, resource) plus optional
   ``structuredContent``. The bookmarklet's existing chat dispatch
   ([chat.py:280-396](../routes/chat.py)) expects a dict that may
   carry a private ``_image: {media_type, data, url?}`` envelope; we
   reshape into that.

   The image envelope is single-slot today, so we pick the first
   image block. Servers that return multiple images per call (rare —
   the rag-enterprise MCP returns one per tool invocation) lose the
   second-and-beyond images for now. Could revisit if a real plugin
   needs multi-image returns; for now the LLM can call again.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any, Callable

# NB: fastmcp wraps the wire-shape ``mcp.types.CallToolResult`` (camelCase
# fields) in its own ``fastmcp.client.client.CallToolResult`` dataclass
# with snake_case attributes + a parsed ``data`` field. The client
# always returns the fastmcp shape, so we import that one. The content
# blocks (TextContent / ImageContent / EmbeddedResource) come from the
# canonical ``mcp.types`` namespace — fastmcp doesn't re-wrap those.
from fastmcp.client.client import CallToolResult
from mcp.types import (
    EmbeddedResource,
    ImageContent,
    TextContent,
    Tool,
)

from app.tools.registry import ToolCtx, ToolSpec

from . import client as mcp_client

_logger = logging.getLogger(__name__)


def synth_tool_spec(
    *,
    connector_id: str,
    remote_tool: Tool,
    tool_prefix: str,
    url_provider: Callable[[str | None], str | None],
    token_provider: Callable[[str | None], str | None],
    host_pattern: str | list[str] | None,
    visibility_check: Callable[[], bool] | None,
    call_check: Callable[[ToolCtx], bool] | None = None,
) -> ToolSpec:
    """Build a ToolSpec that proxies one remote MCP tool.

    Why ``url_provider`` / ``token_provider`` are callables, not values:
    settings can change at runtime (user re-saves the api key, points
    the URL at a different server). We resolve them *at call time* so
    the next chat turn picks up the new value without a backend
    restart. ``url_provider`` additionally takes the page host —
    template connectors (``url_template: "{host}/mcp"``) follow the
    bookmarklet to whichever instance the user is on. The remote *tool
    list* still requires an explicit refresh (per design contract) —
    but credential rotation does not.
    """
    local_name = f"{tool_prefix}{remote_tool.name}"

    async def _handler(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
        if (call_check is not None and not call_check(ctx)) or (
            visibility_check is not None and not visibility_check()
        ):
            return {"ok": False, "error": "mcp_disabled", "message": "MCP binding is disabled, stale, or unavailable for this user/host. Refresh its tool list."}
        url = url_provider(ctx.host)
        # Token follows the resolved URL's host (per-instance keys),
        # not the page host — though for template connectors they agree.
        token = token_provider(url)
        if not url:
            return {
                "ok": False,
                "error": "mcp_not_configured",
                "message": (
                    f"MCP server URL for connector {connector_id!r} is not "
                    "set. Open Settings and fill in the plugin's MCP URL."
                ),
            }
        try:
            result: CallToolResult = await mcp_client.call_tool(
                url, token, remote_tool.name, args or {}
            )
        except Exception as exc:
            # HTTP/client errors may echo Authorization headers. Catalog-bound
            # credentials remain write-only even on failed tool calls.
            message = "MCP tool call failed; check the endpoint and credentials." if call_check is not None else str(exc)
            _logger.warning(
                "mcp call failed: connector=%s tool=%s err=%s",
                connector_id, remote_tool.name, message,
            )
            return {
                "ok": False,
                "error": "mcp_call_failed",
                "message": message,
                "connector": connector_id,
                "remote_tool": remote_tool.name,
            }
        return _normalize_result(result, connector_id, remote_tool.name)

    description = remote_tool.description or f"Remote MCP tool {remote_tool.name!r}"
    # Tag the description so the LLM (and the user reading the tool catalog)
    # knows this isn't a local handler. A one-line prefix is enough; the
    # rest is the server's own docs.
    description = (
        f"[MCP · {connector_id}] {description}".strip()
    )
    schema = _coerce_schema(remote_tool.inputSchema)

    return ToolSpec(
        name=local_name,
        description=description,
        input_schema=schema,
        handler=_handler,
        side="server",
        host_pattern=host_pattern,
        visibility_check=visibility_check,
    )


def _coerce_schema(raw: Any) -> dict[str, Any]:
    """Return a JSON-schema-shaped dict, even when the remote omits one.

    Anthropic / OpenAI / Gemini all reject a tool with no input schema;
    they each want at least ``{"type": "object", "properties": {}}``.
    The rag-enterprise tools all declare proper schemas, but we
    defensively fill in the empty-object shape for any third-party
    server that doesn't.
    """
    if isinstance(raw, dict) and raw.get("type") == "object":
        return raw
    return {"type": "object", "properties": {}, "additionalProperties": True}


def _normalize_result(
    result: CallToolResult,
    connector_id: str,
    remote_tool_name: str,
) -> dict[str, Any]:
    """Re-shape ``CallToolResult`` into the bookmarklet's dict envelope.

    Output schema (mirrors what local Python tools return):

      {
        "ok": bool,                                    # !isError
        "text": str,                                   # concatenated TextContent
        "structured": dict | None,                     # passthrough of structuredContent
        "_image": {media_type, data, url?} | absent,   # first ImageContent if any
        "_resources": [...] | absent,                  # EmbeddedResource passthrough
      }

    The ``_image`` envelope is the private key chat.py looks for to
    surface inline screenshots ([chat.py:317-331](../routes/chat.py)).
    Setting that key lets the LLM literally see the image (Anthropic)
    or get a textual pointer (OpenAI/Gemini) without any further glue.
    """
    text_parts: list[str] = []
    image: dict[str, str] | None = None
    resources: list[dict[str, Any]] = []
    is_error = bool(getattr(result, "is_error", False))

    for block in result.content or []:
        if isinstance(block, TextContent):
            text_parts.append(block.text or "")
        elif isinstance(block, ImageContent):
            if image is None:  # first wins; see module docstring
                image = {
                    "media_type": block.mimeType or "image/png",
                    "data": block.data or "",
                }
        elif isinstance(block, EmbeddedResource):
            # Passed through verbatim — the chat dispatch doesn't
            # natively render these, but the model gets to see them.
            try:
                resources.append(block.model_dump(mode="json"))
            except Exception:
                resources.append({"uri": getattr(block, "uri", None)})
        else:
            # Unknown block type — fall back to its dict shape so the
            # model can at least reason about it.
            try:
                text_parts.append(json.dumps(block.model_dump(mode="json")))
            except Exception:
                text_parts.append(str(block))

    out: dict[str, Any] = {
        "ok": not is_error,
        "text": "\n".join(p for p in text_parts if p).strip(),
    }
    # fastmcp's ``CallToolResult`` carries both the raw wire-shape
    # ``structured_content`` dict AND a Python-parsed ``data`` field
    # (dataclasses/pydantic models built from the tool's output schema).
    # We surface the wire dict only — the parsed-Python form contains
    # objects (Root, etc.) that won't json-encode cleanly when the chat
    # loop serialises the result envelope to send to the LLM.
    structured = getattr(result, "structured_content", None)
    if structured:
        out["structured"] = structured
    if image is not None:
        # Re-validate base64. The MCP wire layer transports base64 strings;
        # decoding now catches corrupt payloads at the boundary rather
        # than letting them blow up later in the Anthropic adapter.
        try:
            base64.b64decode(image["data"], validate=True)
            out["_image"] = image
        except Exception:
            _logger.warning(
                "mcp image data not valid base64; dropping. connector=%s tool=%s",
                connector_id, remote_tool_name,
            )
    if resources:
        out["_resources"] = resources
    if is_error and "error" not in out:
        out["error"] = "mcp_tool_error"
    return out
