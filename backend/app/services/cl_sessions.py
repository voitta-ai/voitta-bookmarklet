"""Per-session metadata registry — feeds the MCP debugging endpoint.

Chainlit's own ``WebsocketSession`` registry tracks sockets + ids but
doesn't surface what *page* each session is on, what its user-agent
is, or when we last heard from it. We keep a parallel dict keyed by
``session_id`` and populate it from ``@cl.on_chat_start`` /
``@cl.on_window_message`` so the tray's "list sessions" view (and the
MCP ``mcp_sessions`` tool) can show meaningful debugging info.

Stays in sync with Chainlit's lifecycle:
* ``record_chat_start`` is called from ``@cl.on_chat_start`` and seeds
  user_agent / created_at.
* ``record_host`` is called from ``@cl.on_window_message`` when the
  bookmarklet posts ``host:<host>`` after mount.
* Sessions are dropped lazily: a session shows up as "stale" if its
  id is no longer in ``chainlit.session.ws_sessions_id``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class PageInfo:
    """Best-effort snapshot of what page a session is attached to.

    All fields except ``session_id`` start ``None`` and get filled
    progressively as window-messages arrive from the FE.
    """

    session_id: str
    host: str | None = None
    url: str | None = None
    title: str | None = None
    user_agent: str | None = None
    created_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    # Free-form metadata the FE might post later (capabilities, plugin
    # list, etc.). Kept open-ended so additions don't need a registry
    # schema change.
    extras: dict[str, Any] = field(default_factory=dict)


_BY_ID: dict[str, PageInfo] = {}

# Registry-wide pointer to the session whose host tab most recently
# gained focus — (session_id, timestamp). Set from the FE's ``focus:1``
# beacon; consumed by the voice assistant to route utterances.
_last_focused: tuple[str, float] | None = None


def record_chat_start(session_id: str, user_agent: str | None = None) -> PageInfo:
    info = _BY_ID.get(session_id)
    if info is None:
        info = PageInfo(session_id=session_id, user_agent=user_agent)
        _BY_ID[session_id] = info
    else:
        info.last_seen = time.time()
        if user_agent and not info.user_agent:
            info.user_agent = user_agent
    return info


def record_host(session_id: str, host: str | None) -> None:
    info = _BY_ID.get(session_id)
    if info is None:
        info = PageInfo(session_id=session_id, host=host)
        _BY_ID[session_id] = info
    else:
        info.host = host
        info.last_seen = time.time()


def record_window_message(session_id: str, message: str) -> None:
    """Parse known ``key:value`` payloads from window-messages and
    update the matching field. Unknown payloads land in ``extras``."""
    info = _BY_ID.get(session_id)
    if info is None:
        info = PageInfo(session_id=session_id)
        _BY_ID[session_id] = info
    info.last_seen = time.time()
    if not isinstance(message, str):
        return
    if message.startswith("focus:"):
        global _last_focused
        now = time.time()
        info.extras["focused_at"] = now
        _last_focused = (session_id, now)
        return
    for key in ("host", "url", "title", "user_agent"):
        prefix = key + ":"
        if message.startswith(prefix):
            setattr(info, key, message[len(prefix):].strip() or None)
            return
    # Unrecognised — stash so we can debug what the FE is sending.
    info.extras.setdefault("raw_messages", []).append(message[:200])


def _live_session_ids() -> set[str]:
    try:
        from chainlit.session import ws_sessions_id  # type: ignore
        return set(ws_sessions_id.keys())
    except Exception:
        return set()


def owner_of(session_id: str) -> str | None:
    """The email a tab is signed in as (server mode), or None."""
    try:
        from chainlit.session import WebsocketSession

        ws = WebsocketSession.get_by_id(session_id)
    except Exception:
        return None
    user = getattr(ws, "user", None) if ws is not None else None
    ident = getattr(user, "identifier", None) if user is not None else None
    return str(ident).strip().lower() if ident else None


def _mine(session_id: str, owner: str | None) -> bool:
    return owner is None or owner_of(session_id) == owner.strip().lower()


def get_active_session(owner: str | None = None) -> PageInfo | None:
    """The session voice input should go to: the last-focused session if
    its socket is still live, else the most recently seen live session,
    else ``None``.

    ``owner`` (server mode, an MCP API key's user): only that user's tabs —
    their last-focused one, else their most recently seen."""
    live = _live_session_ids()
    if _last_focused is not None:
        sid, _ts = _last_focused
        info = _BY_ID.get(sid)
        if info is not None and sid in live and _mine(sid, owner):
            return info
    candidates = [i for i in _BY_ID.values() if i.session_id in live and _mine(i.session_id, owner)]
    if candidates:
        if owner is not None:
            return max(candidates, key=lambda i: (i.extras.get("focused_at", 0), i.last_seen))
        return max(candidates, key=lambda i: i.last_seen)
    return None


def set_active(session_id: str) -> bool:
    """Force ``session_id`` to be the active session (voice routing
    target), as if its tab had just gained focus. Used by the sessions
    window's cmd-tab-style row selection. Returns False if unknown."""
    global _last_focused
    if session_id not in _BY_ID:
        return False
    now = time.time()
    _BY_ID[session_id].extras["focused_at"] = now
    _last_focused = (session_id, now)
    return True


def active_session_id() -> str | None:
    """Session id voice input currently routes to, or None."""
    info = get_active_session()
    return info.session_id if info else None


def forget(session_id: str) -> None:
    _BY_ID.pop(session_id, None)


def get(session_id: str) -> PageInfo | None:
    return _BY_ID.get(session_id)


def all_records() -> list[PageInfo]:
    return list(_BY_ID.values())


def snapshot(owner: str | None = None) -> list[dict[str, Any]]:
    """Wire-friendly dump for the MCP ``mcp_sessions`` tool. Joined with
    Chainlit's own session registry so we can flag ids that no longer
    have a live socket. ``owner``: only that user's tabs (server mode)."""
    live_ids = _live_session_ids()
    out: list[dict[str, Any]] = []
    for info in _BY_ID.values():
        if not _mine(info.session_id, owner):
            continue
        d = asdict(info)
        d["connected"] = info.session_id in live_ids
        out.append(d)
    return out
