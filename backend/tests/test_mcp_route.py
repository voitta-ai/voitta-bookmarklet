"""``/mcp`` access gate tests.

Direct calls to the gate middleware so we don't need a live FastMCP
sub-app stood up. Each test stubs ``mcp_debug_enabled`` and the
request scope to cover one of the three guard layers.
"""

from __future__ import annotations

import pytest
from starlette.requests import Request

from app.routes.mcp import _MCPGate
from app.services import user_settings


class _CapturingApp:
    """Stand-in inner ASGI app — records whether it was called."""

    def __init__(self) -> None:
        self.called = False

    async def __call__(self, scope, receive, send):
        self.called = True
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [],
        })
        await send({"type": "http.response.body", "body": b"OK"})


def _scope(peer: str = "127.0.0.1", origin: str | None = None):
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [],
        "client": (peer, 12345),
    }
    if origin:
        scope["headers"] = [(b"origin", origin.encode())]
    return scope


async def _drive(scope, app=None):
    """Drive the middleware once and return the captured response status."""
    inner = _CapturingApp() if app is None else app
    gate = _MCPGate(inner)
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    await gate(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    return start["status"], inner


@pytest.mark.asyncio
async def test_gate_blocks_when_setting_disabled(monkeypatch):
    # All three switches open the gate; pin every one, or this reads the
    # developer's real settings.json (where MCP tools/chat may well be on).
    monkeypatch.setattr(user_settings, "mcp_debug_enabled", lambda: False)
    monkeypatch.setattr(user_settings, "mcp_tools_enabled", lambda: False)
    monkeypatch.setattr(user_settings, "mcp_chat_enabled", lambda: False)
    status, inner = await _drive(_scope())
    assert status == 403
    assert inner.called is False


@pytest.mark.asyncio
async def test_gate_blocks_non_loopback(monkeypatch):
    monkeypatch.setattr(user_settings, "mcp_debug_enabled", lambda: True)
    status, inner = await _drive(_scope(peer="192.168.1.5"))
    assert status == 403
    assert inner.called is False


@pytest.mark.asyncio
async def test_gate_blocks_browser_origin(monkeypatch):
    monkeypatch.setattr(user_settings, "mcp_debug_enabled", lambda: True)
    status, inner = await _drive(
        _scope(peer="127.0.0.1", origin="https://www.ebay.com"),
    )
    assert status == 403
    assert inner.called is False


@pytest.mark.asyncio
async def test_gate_allows_loopback_no_origin(monkeypatch):
    monkeypatch.setattr(user_settings, "mcp_debug_enabled", lambda: True)
    status, inner = await _drive(_scope(peer="127.0.0.1"))
    assert status == 200
    assert inner.called is True


@pytest.mark.asyncio
async def test_gate_allows_v6_loopback(monkeypatch):
    monkeypatch.setattr(user_settings, "mcp_debug_enabled", lambda: True)
    status, inner = await _drive(_scope(peer="::1"))
    assert status == 200
    assert inner.called is True


def test_sessions_registry_records_window_messages() -> None:
    """The MCP ``mcp_sessions`` tool reads from this registry."""
    from app.services import cl_sessions

    cl_sessions._BY_ID.clear()
    cl_sessions.record_chat_start("sess-1", user_agent="UA-1")
    cl_sessions.record_window_message("sess-1", "host:www.ebay.com")
    cl_sessions.record_window_message("sess-1", "title:Sold listings")
    cl_sessions.record_window_message("sess-1", "url:https://www.ebay.com/sh/lst/sold")

    info = cl_sessions.get("sess-1")
    assert info is not None
    assert info.host == "www.ebay.com"
    assert info.title == "Sold listings"
    assert info.url == "https://www.ebay.com/sh/lst/sold"
    assert info.user_agent == "UA-1"

    snap = cl_sessions.snapshot()
    assert any(s["session_id"] == "sess-1" for s in snap)
    cl_sessions._BY_ID.clear()


def test_mcp_debug_toggle_round_trip(tmp_path, monkeypatch) -> None:
    """The kill-switch helpers round-trip through the settings file."""
    # user_settings resolves its path per call via _settings_dir() →
    # USER_CONFIG_DIR (desktop) — point that at the tmp dir.
    monkeypatch.setattr(user_settings, "USER_CONFIG_DIR", tmp_path)
    from app.services import current_user

    current_user.set_current_email(None)

    assert user_settings.mcp_debug_enabled() is False
    user_settings.set_mcp_debug_enabled(True)
    assert user_settings.mcp_debug_enabled() is True
    user_settings.set_mcp_debug_enabled(False)
    assert user_settings.mcp_debug_enabled() is False
