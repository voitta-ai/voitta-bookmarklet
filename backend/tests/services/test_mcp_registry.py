"""The agent-tools family on /mcp (``vb_*``): an external agent driving the
tool registry the in-app agent uses.

Exercised end to end through FastMCP's in-process client, so listing,
gating, argument validation, session binding and result content are all
the real code paths.
"""

from __future__ import annotations

import json
import sys
from types import ModuleType, SimpleNamespace

import pytest
from fastmcp import Client

from app.services import mcp_registry, mcp_server, user_settings
from app.tools.registry import ToolSpec, registry

SEEN: dict = {}


async def _echo(args, ctx):
    from chainlit.context import context_var

    from app.services.current_user import get_current_email

    cl = context_var.get(None)
    SEEN.update(args=args, session_id=ctx.session_id, host=ctx.host, email=ctx.email,
                cl_session=getattr(getattr(cl, "session", None), "id", None),
                current_email=get_current_email())
    return {"echo": args.get("text"), "_image": {"data": "iVBORw0KGgo=", "media_type": "image/png"}}


@pytest.fixture
def server(monkeypatch):
    """A server with both families on, one fake page (session s1 on
    example.com, signed in as a@b.c) and two test tools."""
    monkeypatch.setattr(user_settings, "mcp_debug_enabled", lambda: True)
    monkeypatch.setattr(user_settings, "mcp_tools_enabled", lambda: True)
    monkeypatch.setattr(user_settings, "mcp_chat_enabled", lambda: True)

    for spec in (
        ToolSpec(name="t_server_echo", description="Echo.\n\nMore detail.",
                 input_schema={"type": "object", "properties": {"text": {"type": "string"}},
                               "required": ["text"], "additionalProperties": False},
                 handler=_echo, side="server"),
        ToolSpec(name="t_page_echo", description="Echo in the page.",
                 input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
                 handler=_echo, side="hybrid", host_pattern="example.com"),
    ):
        monkeypatch.setitem(registry._tools, spec.name, spec)

    ws = SimpleNamespace(id="s1", user=SimpleNamespace(identifier="a@b.c"))
    import chainlit.session

    monkeypatch.setattr(chainlit.session.WebsocketSession, "get_by_id",
                        staticmethod(lambda sid: ws if sid == "s1" else None))
    # A real ChainlitContext needs a live socket emitter.
    monkeypatch.setattr(mcp_registry, "_chainlit_context", lambda ws: SimpleNamespace(session=ws))

    from app.services import cl_sessions

    page = cl_sessions.PageInfo(session_id="s1", host="example.com", url="https://example.com/x")
    monkeypatch.setattr(cl_sessions, "get", lambda sid: page if sid == "s1" else None)
    monkeypatch.setattr(cl_sessions, "get_active_session", lambda: page)
    monkeypatch.setattr(cl_sessions, "snapshot", lambda: [{
        "session_id": "s1", "host": "example.com", "url": page.url, "title": "X",
        "connected": True, "last_seen": 1.0}])

    mcp_server._SERVER = None
    srv = mcp_server.get_server()
    yield srv
    mcp_server._SERVER = None


def _json(result) -> dict:
    return json.loads(result.content[0].text)


async def test_switches_hide_and_refuse_each_family(server, monkeypatch):
    monkeypatch.setattr(user_settings, "mcp_tools_enabled", lambda: False)
    async with Client(server) as c:
        names = {t.name for t in await c.list_tools()}
        assert "mcp_eval" in names and not any(n.startswith("vb_") for n in names)
        with pytest.raises(Exception, match="switched off"):
            await c.call_tool("vb_sessions", {})

    monkeypatch.setattr(user_settings, "mcp_tools_enabled", lambda: True)
    monkeypatch.setattr(user_settings, "mcp_debug_enabled", lambda: False)
    async with Client(server) as c:
        names = {t.name for t in await c.list_tools()}
        # Debugging off leaves the in-app-agent tool: it has its own switch.
        assert {n for n in names if n.startswith("mcp_")} == {"mcp_inject_text"}
        with pytest.raises(Exception, match="switched off"):
            await c.call_tool("mcp_eval", {"session_id": "s1", "js": "1"})


async def test_in_app_agent_has_its_own_switch(server, monkeypatch):
    monkeypatch.setattr(user_settings, "mcp_chat_enabled", lambda: False)
    async with Client(server) as c:
        names = {t.name for t in await c.list_tools()}
        assert "mcp_inject_text" not in names and "mcp_eval" in names
        with pytest.raises(Exception, match="message the in-app agent"):
            await c.call_tool("mcp_inject_text", {"session_id": "s1", "text": "hi"})


async def test_families_are_told_apart(server):
    async with Client(server) as c:
        tools = await c.list_tools()
        info = c.initialize_result
    for t in tools:
        tag = ("[Agent tools]" if t.name.startswith("vb_")
               else "[In-app agent]" if t.name == "mcp_inject_text" else "[Debugging]")
        assert (t.description or "").startswith(tag), t.name
    for family in ("AGENT TOOLS (vb_*)", "DEBUGGING TOOLS (mcp_*)", "IN-APP AGENT (mcp_inject_text)"):
        assert family in info.instructions


async def test_lists_page_tools_and_hides_ask_user(server):
    async with Client(server) as c:
        listed = _json(await c.call_tool("vb_list_tools", {"session_id": "s1"}))
    names = {t["name"]: t for t in listed["tools"]}
    assert names["t_page_echo"]["needs_page"] is True
    assert names["t_server_echo"]["summary"] == "Echo."
    assert "ask_user_question" not in names


async def test_call_runs_in_the_pages_session_as_its_user(server):
    SEEN.clear()
    async with Client(server) as c:
        res = await c.call_tool("vb_call_tool", {"name": "t_page_echo", "arguments": {"text": "hi"}})
    assert not res.is_error
    assert SEEN == {"args": {"text": "hi"}, "session_id": "s1", "host": "example.com",
                    "email": "a@b.c", "cl_session": "s1", "current_email": "a@b.c"}
    assert [b.type for b in res.content] == ["text", "image"]


async def test_invalid_arguments_never_reach_the_tool(server):
    SEEN.clear()
    async with Client(server) as c:
        res = await c.call_tool("vb_call_tool", {"name": "t_server_echo", "arguments": {"txt": 1}},
                                raise_on_error=False)
    assert res.is_error and _json(res)["error"] == "invalid_arguments"
    assert SEEN == {}


async def test_page_tools_need_a_page(server, monkeypatch):
    from app.services import cl_sessions

    monkeypatch.setattr(cl_sessions, "get_active_session", lambda: None)
    async with Client(server) as c:
        listed = _json(await c.call_tool("vb_list_tools", {}))
        res = await c.call_tool("vb_call_tool", {"name": "t_server_echo", "arguments": {"text": "x"}})
        gone = await c.call_tool("vb_call_tool", {"name": "t_page_echo", "session_id": "nope"},
                                 raise_on_error=False)
    assert listed["session_id"] is None and "t_page_echo" not in {t["name"] for t in listed["tools"]}
    assert not res.is_error  # server tools still work with no page open
    assert gone.is_error and _json(gone)["error"] == "no_session"


async def test_instructions_are_the_in_app_prompt(server, monkeypatch):
    from app.services import system_prompt

    monkeypatch.setattr(system_prompt, "compose", lambda host: f"RULES FOR {host}")
    async with Client(server) as c:
        out = _json(await c.call_tool("vb_instructions", {"session_id": "s1"}))
    assert out["instructions"].endswith("RULES FOR example.com")
    assert "ask_user_question" in out["instructions"]  # tells the agent what differs
