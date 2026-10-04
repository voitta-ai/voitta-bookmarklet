"""browser_eval refuses scripts that reload or navigate the tab: the
bookmarklet lives in the page, so a navigation ends the session and the
call never returns."""
from __future__ import annotations

import pytest

from app.tools.browser import browser_eval


@pytest.mark.parametrize("js", [
    "location.reload()", "window.location.reload(true)", 'location.href = "/x"',
    "window.location = url", "location.assign(u)", "document.location = u",
])
async def test_navigation_is_refused(js, monkeypatch):
    async def never(*a, **kw):
        raise AssertionError("must not reach the tab")
    monkeypatch.setattr(browser_eval, "call_browser", never)
    out = await browser_eval._handler({"js": js}, None)
    assert out["error"] == "navigation_blocked"


@pytest.mark.parametrize("js", [
    "return location.href", "if (location.href == a) return 1",
    'history.pushState({}, "", "/a")', "const p = location.pathname",
])
async def test_reads_and_spa_routing_pass(js, monkeypatch):
    async def ok(name, args, ctx, timeout_ms):
        return {"ok": True, "result": 1}
    monkeypatch.setattr(browser_eval, "call_browser", ok)
    assert (await browser_eval._handler({"js": js}, None))["ok"]


async def test_explicit_permission_lets_navigation_through(monkeypatch):
    async def ok(name, args, ctx, timeout_ms):
        return {"ok": True}
    monkeypatch.setattr(browser_eval, "call_browser", ok)
    out = await browser_eval._handler({"js": "location.reload()", "allow_navigation": True}, None)
    assert out["ok"]
