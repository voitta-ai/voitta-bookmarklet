"""Who is calling /mcp, and what may they do — server mode.

On the multi-user server, Claude Code (or any MCP client) authenticates with a per-user API key:
``Authorization: Bearer vbk_…`` (see :func:`app.services.users.create_key`). The key resolves to
its user and its scopes; every tool call then runs as that user and only sees that user's tabs.

The HTTP guard checks the key once per request. The MCP tool handlers don't necessarily run in the
request's task (FastMCP's session manager owns them), so the family gate re-resolves the caller
from the request headers on each call and pins it here for the duration of that call.

On the desktop app there are no keys: /mcp stays loopback-only, one user, no caller.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class Caller:
    email: str
    scopes: frozenset[str]


_caller: ContextVar[Caller | None] = ContextVar("voitta_mcp_caller", default=None)


def bearer(headers: Mapping[Any, Any]) -> str | None:
    """The bearer token from ``Authorization`` (str or bytes headers), or None."""
    raw = headers.get("authorization")
    if raw is None:
        raw = headers.get(b"authorization")
    if isinstance(raw, bytes):
        raw = raw.decode("latin-1")
    scheme, _, token = str(raw or "").strip().partition(" ")
    return token.strip() or None if scheme.lower() == "bearer" else None


def caller_from_headers(headers: Mapping[Any, Any]) -> Caller | None:
    from app.services import users

    hit = users.key_user(bearer(headers))
    if hit is None:
        return None
    user, scopes = hit
    return Caller(email=user["email"], scopes=frozenset(scopes))


def set_caller(caller: Caller | None):
    return _caller.set(caller)


def reset_caller(token) -> None:
    _caller.reset(token)


def current_caller() -> Caller | None:
    return _caller.get()


def owner_filter() -> str | None:
    """The email whose tabs this call may see/drive, or None for no filter (desktop)."""
    c = _caller.get()
    return c.email if c else None
