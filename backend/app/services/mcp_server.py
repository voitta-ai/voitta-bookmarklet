"""Embedded MCP server — debugging tools and agent tools.

Two tool families on one server, told apart by name prefix, a tag at the
start of every description, and the server instructions:

  * ``mcp_*`` — **debugging** (this module): inspect and poke the app —
    raw JS in a tab, page HTML, screenshots, devtools capture.
  * ``vc_*`` — **agent tools** (:mod:`app.services.mcp_registry`): the
    tool registry the in-app agent uses, for an external agent to drive.

Mounted at ``/mcp`` (streamable-HTTP transport on the existing FastAPI
listener — no second port). Gated by:

  * per family, its own user setting (tray-bar toggles):
    ``mcpDebugEnabled`` for ``mcp_*``, ``mcpToolsEnabled`` for ``vc_*`` —
    a family that's off is neither listed nor callable (``_FamilyGate``)
  * Loopback-only peer (127.0.0.1 / ::1)
  * No browser ``Origin`` header (use a CLI/desktop MCP client, not
    a tab — defends against drive-by JS calls from a malicious page)

The tools target individual bookmarklet sessions by Chainlit session
id. Each tool that needs to talk to the FE looks up the session via
``WebsocketSession.get_by_id``, then dispatches a ``call_fn`` over the
session's existing socket — same mechanism the agent loop uses for
browser tools, just driven from outside an LLM turn.

Tools shipped:

* ``mcp_sessions``       — list every live bookmarklet session
* ``mcp_page``           — full ``<html>`` outerHTML of one session's page
* ``mcp_eval``           — arbitrary JS in a session's page
* ``mcp_screenshot``     — capture the report pane as base64 PNG

The chat-injection / chat-state tools from the legacy CLI are not
ported — they require deeper Chainlit-client surgery and aren't
needed for the typical debugging use case.
"""

from __future__ import annotations

import logging
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware, MiddlewareContext


_log = logging.getLogger(__name__)
_SERVER: FastMCP | None = None


def _err(kind: str, message: str, **extra: Any) -> dict:
    out: dict[str, Any] = {"ok": False, "error": kind, "message": message}
    out.update(extra)
    return out


# Default per-call timeout for MCP -> FE round-trips. Chainlit's own
# ``send_call_fn`` defaults to 300s which "hangs" from the user's
# perspective when the FE isn't responding for any reason. 15s is
# enough for any reasonable primitive (the longest is the screenshot
# auto-size dance at ~5s) and short enough to surface failure quickly.
_MCP_CALL_TIMEOUT_S = 15

# Liveness ping sent ahead of any call that may wait longer than this. A tab
# whose socket is still open but whose page has stopped answering (browser
# froze/throttled a background tab, page JS wedged) otherwise costs the full
# call timeout per attempt, which reads as a silent hang from the client.
_PING_TIMEOUT_S = 3
_PING_PRIMITIVE = "get_page_title"

_NOT_RESPONDING = (
    "The tab is still connected but its page stopped answering (no reply to a "
    "{s}s ping). Usually the browser froze or throttled it as a background "
    "tab, or the page is stuck. Bring the tab to the front — reload it if that "
    "doesn't help — then retry. Other sessions: mcp_sessions."
)


async def _call_in_session(
    session_id: str,
    primitive: str,
    args: dict,
    *,
    timeout_s: float = _MCP_CALL_TIMEOUT_S,
) -> dict:
    """Dispatch ``call_fn`` into the Chainlit session whose id matches.

    Builds an emitter against the session and awaits the FE's ACK.
    Returns a uniform envelope so callers can dispatch error / payload
    cases without worrying about Chainlit's internals.

    The dispatch is wrapped in ``asyncio.wait_for(..., timeout_s)`` so
    a non-responding FE surfaces as a clear ``no_ack`` error within
    seconds instead of hanging for 5 minutes (Chainlit's
    ``send_call_fn`` default is 300s and silently returns None on
    timeout). Override per-call by passing ``timeout_s`` — e.g.
    ``mcp_screenshot`` does this because the auto-size dance can take
    longer than the default.
    """
    import asyncio
    import time

    _log.info("MCP▶ _call_in_session START session=%s primitive=%s timeout=%ss",
              session_id, primitive, timeout_s)

    try:
        from chainlit.emitter import ChainlitEmitter
        from chainlit.session import WebsocketSession
    except Exception as exc:
        _log.error("MCP▶ chainlit import FAILED: %s", exc)
        return _err("chainlit_unavailable", str(exc), session_id=session_id)

    _log.info("MCP▶ looking up WebsocketSession for %s", session_id)
    session = WebsocketSession.get_by_id(session_id)
    if session is None:
        _log.warning("MCP▶ no WebsocketSession for %s — known sessions: %s",
                     session_id,
                     [s.id for s in WebsocketSession.get_all()] if hasattr(WebsocketSession, 'get_all') else "?")
        return _err("no_session",
                    f"no live Chainlit session with id {session_id!r}",
                    session_id=session_id)

    socket_id = getattr(session, "socket_id", None)
    _log.info("MCP▶ found session=%s socket_id=%s type=%s",
              session_id, socket_id, type(session).__name__)

    # Fast-fail: check socket.io manager before committing to sio.call().
    # sio.call() on a dead socket blocks the event loop for the full timeout
    # and resists asyncio.wait_for cancellation.
    try:
        from chainlit.server import sio  # type: ignore
        sid = socket_id or session_id
        # Chainlit's socket lives on the default namespace.
        connected = sio.manager.is_connected(sid, "/")
        rooms = list(sio.manager.get_rooms(sid, "/") or [])
        _log.info("MCP▶ sio.manager.is_connected(%s)=%s rooms=%s", sid, connected, rooms)
        if not connected:
            _log.warning("MCP▶ socket %s not connected in sio.manager — fast-fail", sid)
            return _err(
                "stale_session",
                f"session {session_id!r} socket {sid!r} is not connected "
                f"in socket.io manager — tab is closed or disconnected",
                session_id=session_id,
            )
    except Exception as exc:
        _log.warning("MCP▶ sio liveness check failed (%s) — trying ws_sessions_id", exc)
        try:
            from chainlit.session import ws_sessions_id  # type: ignore
            in_index = session_id in ws_sessions_id
            _log.info("MCP▶ ws_sessions_id check: session_id in index=%s (total=%d)",
                      in_index, len(ws_sessions_id))
            if not in_index:
                return _err("stale_session",
                            f"session {session_id!r} evicted from ws_sessions_id",
                            session_id=session_id)
        except Exception as exc2:
            _log.warning("MCP▶ ws_sessions_id check also failed (%s) — proceeding", exc2)

    # A connected socket doesn't mean a live page. Ping first so a frozen tab
    # fails in ~_PING_TIMEOUT_S instead of the full timeout_s.
    if primitive != _PING_PRIMITIVE and timeout_s > _PING_TIMEOUT_S:
        ping = await _call_in_session(
            session_id, _PING_PRIMITIVE, {}, timeout_s=_PING_TIMEOUT_S,
        )
        if ping.get("error") == "no_ack":
            return _err(
                "tab_not_responding",
                _NOT_RESPONDING.format(s=_PING_TIMEOUT_S),
                session_id=session_id, primitive=primitive,
            )
        if not ping.get("ok"):
            return ping

    _log.info("MCP▶ building ChainlitEmitter and calling send_call_fn primitive=%s", primitive)
    t0 = time.perf_counter()
    try:
        emitter = ChainlitEmitter(session)
        _log.info("MCP▶ emitter built — awaiting send_call_fn primitive=%s timeout=%ss", primitive, timeout_s)
        # send_call_fn wraps sio.call(), which uses asyncio.wait_for internally and
        # times out after timeout_s with socketio.TimeoutError. However, the
        # post-timeout cleanup inside send_call_fn (send_timeout, clear) also calls
        # sio.emit() which has no timeout of its own and can hang if the socket is
        # stale. The outer asyncio.wait_for here (timeout_s + 5s grace) is a backstop
        # that cancels the whole coroutine if Chainlit's internal cleanup stalls.
        result = await asyncio.wait_for(
            emitter.send_call_fn(primitive, args, timeout=int(timeout_s)),
            timeout=timeout_s + 5,
        )
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        _log.info("MCP▶ send_call_fn returned in %dms result=%s", elapsed_ms, type(result).__name__)
    except asyncio.TimeoutError:
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        _log.warning("MCP▶ outer asyncio.wait_for timeout after %dms (inner=%ss + 5s grace) primitive=%s session=%s",
                     elapsed_ms, timeout_s, primitive, session_id)
        return _err(
            "no_ack",
            f"outer asyncio timeout after {elapsed_ms}ms (inner={timeout_s}s + 5s grace) — "
            f"socket_id={socket_id!r} — Chainlit post-timeout cleanup likely stalled",
            session_id=session_id, primitive=primitive, elapsed_ms=elapsed_ms,
        )
    except Exception as exc:
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        # sio.call() raises socketio.exceptions.TimeoutError (not asyncio.TimeoutError)
        # when the FE doesn't ack within timeout_s. Chainlit catches it internally
        # and returns None from send_call_fn, so we rarely see it here — but guard
        # anyway.
        exc_name = type(exc).__name__
        if "Timeout" in exc_name:
            _log.warning("MCP▶ inner timeout (%s) after %dms primitive=%s session=%s",
                         exc_name, elapsed_ms, primitive, session_id)
            return _err(
                "no_ack",
                f"no ack from FE after {timeout_s}s — socket_id={socket_id!r}",
                session_id=session_id, primitive=primitive, elapsed_ms=elapsed_ms,
            )
        _log.exception("MCP▶ send_call_fn(%s) raised %s after %dms", primitive, exc_name, elapsed_ms)
        return _err("dispatch_failed", str(exc), session_id=session_id)

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    if result is None:
        _log.warning("MCP▶ result is None (chainlit-side timeout) primitive=%s elapsed=%dms",
                     primitive, elapsed_ms)
        return _err("no_ack",
                    f"no reply from the tab within {timeout_s:g}s ({primitive}). "
                    "The page answered a ping just before, so the primitive itself "
                    "is slow or stuck — not the tab.",
                    session_id=session_id, primitive=primitive, elapsed_ms=elapsed_ms)

    _log.info("MCP▶ SUCCESS primitive=%s elapsed=%dms", primitive, elapsed_ms)
    if not isinstance(result, dict):
        return {"ok": True, "session_id": session_id, "result": result}
    out = dict(result)
    out.setdefault("ok", True)
    out["session_id"] = session_id
    out["elapsed_ms"] = elapsed_ms
    return out


def _eval_timeout_s(await_ms: int) -> float:
    """Round-trip budget for ``eval_js``: the script's own deadline plus
    transport grace. A fixed 15 s cut off scripts the caller had explicitly
    allowed to run longer — the tab kept running them, the caller saw a
    timeout."""
    return max(float(_MCP_CALL_TIMEOUT_S), max(0, int(await_ms)) / 1000 + 5)


_INSTRUCTIONS = """\
Voitta Compute — the local app behind the Voitta bookmarklet. Its tools
come in two families; use the one that matches the job.

AGENT TOOLS (vc_*) — do the user's work. The same tool registry the in-app
Voitta agent uses (reports, LinkedIn, eBay, Google, projects, Python via
run_script, …), so you can act as that agent. Workflow:
  1. vc_sessions — pick the page (bookmarklet tab) to work on.
  2. vc_instructions — read the agent's rules for that page. Follow them.
  3. vc_list_tools, then vc_describe_tools for the tools you'll use.
  4. vc_call_tool(name, arguments, session_id).

DEBUGGING TOOLS (mcp_*) — inspect and poke the app itself: raw JavaScript
in a tab (mcp_eval), the page HTML, screenshots, console/network capture.
For developing and diagnosing Voitta; not for doing user tasks — the
agent tools carry validation, auth and the rules that raw JavaScript
bypasses.

IN-APP AGENT (mcp_inject_text) — post a message into a tab's chat; the
in-app Voitta agent runs it as the user's own message, on the app's
model access, and the user sees it in their chat pane.

Each family has its own switch in the Voitta tray → Settings; a family
that is switched off is not listed. Tool descriptions are tagged
[Agent tools], [Debugging] or [In-app agent].
"""


# Tools that post into the in-app agent's chat. Their own family: a posted
# message runs a full in-app turn on the user's model access, with the
# user's authority — a wider grant than inspecting the app.
_CHAT_TOOLS = frozenset({"mcp_inject_text"})


class _FamilyGate(Middleware):
    """Hide and refuse each tool family by its own setting, read per request
    so the tray switches apply without a restart."""

    @staticmethod
    def _family(name: str) -> tuple[bool, str]:
        """(enabled, the switch's label in the tray) for a tool name."""
        from app.services import user_settings as us
        from app.services.mcp_registry import FAMILY_PREFIX

        if name in _CHAT_TOOLS:
            return us.mcp_chat_enabled(), "Let external agents message the in-app agent"
        if name.startswith(FAMILY_PREFIX):
            return us.mcp_tools_enabled(), "Expose agent tools to external agents"
        return us.mcp_debug_enabled(), "Enable MCP debugging tools"

    async def on_list_tools(self, context: MiddlewareContext, call_next):
        tools = await call_next(context)
        return [t for t in tools if self._family(t.name)[0]]

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        name = context.message.name
        enabled, switch = self._family(name)
        if not enabled:
            raise ToolError(f"{name} is switched off — turn on '{switch}' in the Voitta "
                            "tray → Settings.")
        return await call_next(context)


def get_server() -> FastMCP:
    """Lazy singleton — FastMCP instance with both tool families: the
    ``mcp_*`` debugging tools below and the ``vc_*`` agent tools
    (:mod:`app.services.mcp_registry`)."""
    global _SERVER
    if _SERVER is not None:
        return _SERVER

    mcp = FastMCP("voitta-compute", instructions=_INSTRUCTIONS, middleware=[_FamilyGate()])

    from app.services.mcp_registry import register as register_agent_tools

    register_agent_tools(mcp)

    @mcp.tool()
    async def mcp_session_check(session_id: str, timeout_s: int = 5) -> dict:
        """[Debugging] Diagnostic round-trip probe.

        Calls the lightest-possible primitive (``get_page_title``) with
        a short timeout. Returns ``{ok: true, ms, title}`` if the FE
        responded, or ``{ok: false, error: 'no_ack'|...}`` with the
        elapsed time if not. Use this BEFORE running mcp_eval / mcp_page
        / mcp_screenshot if the chat seems healthy but those hang —
        narrows down whether the call_fn round-trip works at all from
        outside an active chat turn.

        Common failure modes the result distinguishes:
          • ``no_session``       — that session id isn't registered
          • ``no_ack``           — FE didn't ack within ``timeout_s``
                                   (CallFnRouter not mounted, socket
                                   subscription dropped, FE primitive
                                   threw without calling back)
          • ``dispatch_failed``  — chainlit/socket.io raised mid-emit
          • ``ok: true``         — everything's wired
        """
        return await _call_in_session(
            session_id, "get_page_title", {}, timeout_s=float(timeout_s),
        )

    @mcp.tool()
    async def mcp_sessions() -> dict:
        """[Debugging] List every bookmarklet session the backend currently knows about.

        Returns ``{count, sessions: [{session_id, connected, host, url,
        title, user_agent, created_at, last_seen, extras}]}``. The
        ``session_id`` from each entry is what the other ``mcp_*``
        tools take as input.
        """
        from app.services import cl_sessions

        records = cl_sessions.snapshot()
        return {"count": len(records), "sessions": records}

    @mcp.tool()
    async def mcp_page(session_id: str) -> dict:
        """[Debugging] Return URL + title + path to a file containing the full page HTML.

        The HTML is written to a dump file (cleared on app restart) rather
        than returned inline — page DOMs can exceed 1 MB. Read the file
        with Claude Code's Read tool.

        Returns ``{ok, url, title, host, html_bytes, file, fetched_at_ms}``.
        """
        _log.info("MCP▶ mcp_page called session=%s", session_id)
        from app.services import mcp_dumps

        payload = await _call_in_session(session_id, "get_page_dump", {})
        if not payload.get("ok"):
            return payload

        html = payload.get("html") or ""
        dump_path = mcp_dumps.write_text(session_id, "page", html, ".html")

        return {
            "ok": True,
            "session_id": session_id,
            "url": payload.get("url"),
            "title": payload.get("title"),
            "host": payload.get("host"),
            "pathname": payload.get("pathname"),
            "search": payload.get("search"),
            "hash": payload.get("hash"),
            "user_agent": payload.get("user_agent"),
            "html_bytes": len(html),
            "file": str(dump_path),
            "fetched_at_ms": payload.get("ts"),
        }

    @mcp.tool()
    async def mcp_eval(
        session_id: str,
        js: str,
        await_ms: int = 30_000,
    ) -> dict:
        """[Debugging] Run arbitrary JavaScript in a bookmarklet tab. For
        inspecting the app — to do a user's task use the agent tools
        (vc_call_tool), which carry validation and the agent's rules.

        The code body is wrapped in an ``AsyncFunction`` — ``await`` at
        top level works, and ``return X`` sends ``X`` back. Console
        output is captured into the ``logs`` array. Throws come back
        as ``{ok: false, error: 'eval_threw', message, stack, logs}``
        rather than transport errors.

        ``await_ms`` is a hard timeout: a runaway script aborts at
        this deadline so the call_fn round-trip can't hang. The
        round-trip itself waits ``await_ms`` plus a few seconds of
        transport grace (never less than the default call timeout).
        """
        _log.info("MCP▶ mcp_eval called session=%s await_ms=%s js_len=%d", session_id, await_ms, len(js or ""))
        if not js or not js.strip():
            return _err("bad_request", "js is required", session_id=session_id)
        return await _call_in_session(
            session_id, "eval_js", {"js": js, "await_ms": int(await_ms)},
            timeout_s=_eval_timeout_s(await_ms),
        )

    @mcp.tool()
    async def mcp_inject_text(session_id: str, text: str) -> dict:
        """[In-app agent] Post a user message into a bookmarklet chat and run it.
        The in-app agent handles it as the user's own message, on the
        app's model access.

        Same path the voice assistant uses (``submit_user_text``
        primitive → Composer's sendMessage → ``@cl.on_message``), minus
        the microphone. The message renders in the chat pane and runs a
        normal LLM turn. Returns ``{ok: true}`` once the FE accepted it,
        or ``{ok: false, error: 'busy'|'empty_text'|...}``.
        """
        _log.info("MCP▶ mcp_inject_text called session=%s text_len=%d", session_id, len(text or ""))
        if not text or not text.strip():
            return _err("bad_request", "text is required", session_id=session_id)
        return await _call_in_session(session_id, "submit_user_text", {"text": text})

    @mcp.tool()
    async def mcp_screenshot(session_id: str) -> dict:
        """[Debugging] Silent screenshot of the currently-mounted report pane.

        Calls the same ``screenshot_report`` primitive the chat LLM
        uses, but bypasses chat — no message in the pane, no LLM turn.
        Returns base64 PNG + size metadata in the ``_image`` envelope.
        """
        _log.info("MCP▶ mcp_screenshot called session=%s", session_id)
        payload = await _call_in_session(session_id, "screenshot_report", {})
        if not payload.get("ok"):
            return payload
        return {
            "ok": True,
            "session_id": session_id,
            "width": payload.get("width"),
            "height": payload.get("height"),
            "image": payload.get("_image"),
        }

    @mcp.tool()
    async def mcp_devtools_install(session_id: str) -> dict:
        """[Debugging] Install console / network / error interceptors in a bookmarklet tab.

        Wraps ``console.*``, ``fetch``, ``XMLHttpRequest``, ``window.onerror``
        and ``unhandledrejection`` with thin shims that write to a ring buffer
        (max 300 entries per category, request/response bodies truncated at 8 KB).

        Safe to call multiple times — idempotent. Only captures activity
        that happens AFTER installation.

        Returns ``{ok, already_installed}``.
        """
        _log.info("MCP▶ mcp_devtools_install called session=%s", session_id)
        return await _call_in_session(session_id, "install_devtools_capture", {})

    @mcp.tool()
    async def mcp_devtools_read(
        session_id: str,
        kind: str = "all",
        limit: int = 100,
        clear: bool = False,
    ) -> dict:
        """[Debugging] Read captured devtools data, written to a file for Claude Code to read.

        Requires ``mcp_devtools_install`` to have been called first.

        Args:
          kind   — "console" | "network" | "errors" | "all" (default: "all")
          limit  — max entries per category (default: 100, max: 300)
          clear  — flush returned entries from the buffer after reading

        Returns ``{ok, installed, file, counts, summary}`` where:
          file    — path to a JSON dump file; read with Claude Code's Read tool
          counts  — {console, network, errors} entry counts in this snapshot
          summary — brief description of what's in the file

        File schema: ``{console?: [...], network?: [...], errors?: [...]}``
          console entries: {ts, level, message, stack?}
          network entries: {ts, method, url, status, duration_ms,
                            req_headers, res_headers, req_body, res_body, error?}
          error   entries: {ts, message, source?, lineno?, colno?, stack?}
        """
        _log.info("MCP▶ mcp_devtools_read called session=%s kind=%s limit=%s clear=%s", session_id, kind, limit, clear)
        from app.services import mcp_dumps

        if kind not in ("console", "network", "errors", "all"):
            return _err("bad_request", f"kind must be console|network|errors|all, got {kind!r}")

        payload = await _call_in_session(
            session_id, "get_devtools_data",
            {"kind": kind, "limit": min(limit, 300), "clear": clear},
        )
        if not payload.get("ok"):
            return payload

        data: dict = {}
        counts: dict[str, int] = {}
        for cat in ("console", "network", "errors"):
            if cat in payload:
                data[cat] = payload[cat]
                counts[cat] = len(payload[cat])

        dump_path = mcp_dumps.write_json(session_id, f"devtools_{kind}", data)

        parts = [f"{v} {k}" for k, v in counts.items() if v]
        summary = f"{', '.join(parts)} entries" if parts else "empty"

        return {
            "ok": True,
            "session_id": session_id,
            "installed": payload.get("installed", False),
            "file": str(dump_path),
            "counts": counts,
            "summary": summary,
        }

    @mcp.tool()
    async def mcp_devtools_clear(session_id: str) -> dict:
        """[Debugging] Clear all captured devtools data in a bookmarklet tab.

        Empties the console, network, and error ring buffers without
        uninstalling the interceptors. Useful before triggering a specific
        action you want to observe cleanly.
        """
        _log.info("MCP▶ mcp_devtools_clear called session=%s", session_id)
        return await _call_in_session(session_id, "clear_devtools_data", {})

    _SERVER = mcp
    return mcp
