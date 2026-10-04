"""Tab round-trips from the debug MCP server (``_call_in_session``).

* A tab whose socket is open but whose page no longer answers must fail fast
  with ``tab_not_responding`` — not wait out the full call timeout, which
  reads as a silent hang from the MCP client.
* ``mcp_eval`` must wait as long as the ``await_ms`` it was given, not a
  fixed 15 s.
"""

from __future__ import annotations

import asyncio
import sys
import time
from types import ModuleType, SimpleNamespace

import chainlit.emitter
import chainlit.session
import pytest

from app.services import mcp_server


@pytest.fixture
def tab(monkeypatch):
    """A fake connected session; ``tab.alive`` decides whether the page
    answers. Records each (primitive, timeout) sent."""
    state = SimpleNamespace(alive=True, socket_up=True, calls=[])
    session = SimpleNamespace(id="s1", socket_id="sock1")

    monkeypatch.setattr(chainlit.session.WebsocketSession, "get_by_id",
                        staticmethod(lambda sid: session if sid == "s1" else None))
    # Stub module, not the real one: importing chainlit.server boots
    # Chainlit's app and hangs test collection. Same (sid, namespace)
    # signature as python-socketio's BaseManager.
    fake_server = ModuleType("chainlit.server")
    fake_server.sio = SimpleNamespace(manager=SimpleNamespace(
        is_connected=lambda sid, namespace: state.socket_up,
        get_rooms=lambda sid, namespace: []))
    monkeypatch.setitem(sys.modules, "chainlit.server", fake_server)

    class FakeEmitter:
        def __init__(self, _session):
            pass

        async def send_call_fn(self, name, args, timeout):
            state.calls.append((name, timeout))
            if not state.alive:
                # Chainlit swallows the socket.io timeout and returns None.
                await asyncio.sleep(0.05)
                return None
            return {"title": "t"} if name == "get_page_title" else {"result": name}

    monkeypatch.setattr(chainlit.emitter, "ChainlitEmitter", FakeEmitter)
    return state


async def test_frozen_tab_fails_fast_with_clear_error(tab):
    tab.alive = False
    t0 = time.perf_counter()
    out = await mcp_server._call_in_session("s1", "eval_js", {}, timeout_s=60)
    assert out["error"] == "tab_not_responding"
    assert "front" in out["message"]
    # Only the short ping went out; the 60 s call was never attempted.
    assert tab.calls == [("get_page_title", mcp_server._PING_TIMEOUT_S)]
    assert time.perf_counter() - t0 < 5


async def test_live_tab_pings_then_runs(tab):
    out = await mcp_server._call_in_session("s1", "eval_js", {}, timeout_s=60)
    assert out["ok"] is True and out["result"] == "eval_js"
    assert tab.calls == [("get_page_title", 3), ("eval_js", 60)]


async def test_ping_itself_is_not_pinged(tab):
    await mcp_server._call_in_session("s1", "get_page_title", {}, timeout_s=5)
    assert tab.calls == [("get_page_title", 5)]


async def test_disconnected_socket_fails_without_any_call(tab):
    tab.socket_up = False
    out = await mcp_server._call_in_session("s1", "eval_js", {}, timeout_s=60)
    assert out["error"] == "stale_session"
    assert tab.calls == []


async def test_unknown_session_skips_ping(tab):
    out = await mcp_server._call_in_session("nope", "eval_js", {})
    assert out["error"] == "no_session"
    assert tab.calls == []


def test_eval_timeout_follows_await_ms():
    assert mcp_server._eval_timeout_s(60_000) == 65
    assert mcp_server._eval_timeout_s(1_000) == mcp_server._MCP_CALL_TIMEOUT_S
    assert mcp_server._eval_timeout_s(-5) == mcp_server._MCP_CALL_TIMEOUT_S
