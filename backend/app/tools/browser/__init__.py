"""Helper used by hybrid tools to invoke a browser-side primitive.

The page is reached through a :class:`BrowserTransport` carried on
``ToolCtx.browser``. When none is set, the default is
:class:`ChainlitTransport`: ``cl.CopilotFunction(name, args).acall()``
round-trips to the React client's ``call_fn`` socket and back. The FE
router ([frontend/src/lib/CallFnRouter.tsx]) looks up ``name`` in the
``primitives`` map ([frontend/src/lib/primitives.ts] — extended by
plugin ``widget.ts`` files via ``registerPrimitive``) and ACKs. The
eval runner sets an unattended worker (app.eval.browser_worker) or
:class:`UnavailableTransport` instead.

This module wraps that with the same error envelope the source repo
uses, so plugin tools port verbatim.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

import chainlit as cl

from app.tools.registry import ToolCtx

logger = logging.getLogger(__name__)


class BrowserToolError(RuntimeError):
    """Raised by :func:`call_browser` when a browser primitive fails."""

    def __init__(self, kind: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.details = details


class BrowserTransport(Protocol):
    """Delivers one primitive call to a page and returns its payload.
    Raises :class:`BrowserToolError` on failure. Deadlines are the
    transport's job, not the page's."""

    async def call(self, name: str, args: dict[str, Any], timeout_ms: int) -> Any: ...


class ChainlitTransport:
    """The live chat path: the bookmarklet in the user's tab, over the
    Chainlit socket. Chainlit's own ack-or-fail loop bounds the call."""

    async def call(self, name: str, args: dict[str, Any], timeout_ms: int) -> Any:
        try:
            return await cl.CopilotFunction(name=name, args=args).acall()
        except Exception as exc:  # noqa: BLE001 — chainlit raises various types
            raise BrowserToolError(
                "dispatch_failed",
                f"browser primitive {name!r} dispatch failed: {exc}",
            ) from exc


class UnavailableTransport:
    """No page is attached: every call fails explicitly, never silently."""

    def __init__(self, reason: str) -> None:
        self.reason = reason

    async def call(self, name: str, args: dict[str, Any], timeout_ms: int) -> Any:
        raise BrowserToolError(
            "tool_unavailable", f"browser primitive {name!r} unavailable: {self.reason}",
        )


_CHAINLIT = ChainlitTransport()


async def call_browser(
    name: str,
    args: dict[str, Any] | None,
    ctx: ToolCtx,
    timeout_ms: int = 15_000,
) -> Any:
    """Invoke browser primitive ``name`` with ``args`` and return its
    payload, through ``ctx.browser`` (Chainlit when unset). The
    transport enforces ``timeout_ms``; the Chainlit transport leaves it
    to Chainlit's own ack-or-fail loop.

    Raises :class:`BrowserToolError` when:

    * The transport round-trip itself fails (no browser attached,
      socket closed, deadline passed, etc.).
    * The primitive returns a dict with a top-level ``error`` field —
      treated as a structured failure and re-raised as
      ``BrowserToolError`` so the wrapping tool handler can surface
      it to the model uniformly.
    """
    transport = ctx.browser or _CHAINLIT
    res = await transport.call(name, args or {}, timeout_ms)

    # Some primitives return a `{ error: "..." }` envelope on failure
    # rather than throwing — turn that into a BrowserToolError too so
    # callers don't need two code paths.
    if isinstance(res, dict) and res.get("error") and not res.get("ok", False):
        err = res.get("error")
        kind = "primitive_error"
        message = str(err) if not isinstance(err, dict) else str(err.get("message") or err)
        details: Any = res
        if isinstance(err, dict):
            kind = str(err.get("kind") or kind)
        raise BrowserToolError(kind, message, details)

    return res


__all__ = [
    "BrowserToolError", "BrowserTransport", "ChainlitTransport",
    "UnavailableTransport", "call_browser",
]
