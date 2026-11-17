"""The panel ↔ server link: how API calls reach the user's StockTrak tab.

The hg panel (running inside app.stocktrak.com) opens ``wss://hg.voitta.ai/hg/link`` and says
hello with its key. API calls for that key are sent down the socket as ``{t:"call", id, name,
args}``; the panel runs the Hedgehog browser primitive and answers ``{t:"result"|"error", id, …}``.

Routing is by key only, so a key can never reach another key's tab. With several tabs open under
one key, calls go to the most recently active one (the panel reports focus/visibility/clicks) and
``/v1/status`` shows them all — no ambiguity. Calls for one key run one at a time: it's a single
browser tab and StockTrak's bot protection doesn't like bursts.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

from starlette.websockets import WebSocket, WebSocketDisconnect

from app.hg import store
from app.tools.browser import BrowserToolError

# Only the Hedgehog primitives cross this link (the panel enforces the same on its side).
PRIMITIVE_PREFIX = "hedgehog_"
HELLO_TIMEOUT_S = 10
DEFAULT_ORIGINS = r"^https://([a-z0-9-]+\.)*stocktrak\.com$"


def origin_allowed(origin: str | None) -> bool:
    pattern = os.environ.get("VOITTA_HG_PANEL_ORIGINS", DEFAULT_ORIGINS)
    return bool(origin) and re.match(pattern, origin.strip().lower()) is not None


@dataclass(eq=False)
class Tab:
    key_id: int
    ws: WebSocket
    page: str = ""
    connected_at: float = field(default_factory=time.time)
    active_at: float = field(default_factory=time.time)
    pending: dict[int, asyncio.Future] = field(default_factory=dict)
    retired: bool = False
    tid: int = field(default_factory=lambda: next(_tab_ids))

    def public(self) -> dict[str, Any]:
        return {"id": self.tid, "page": self.page, "connected_at": self.connected_at, "active_at": self.active_at}


_tabs: dict[int, list[Tab]] = {}
_locks: dict[int, asyncio.Lock] = {}
_ids = itertools.count(1)
_tab_ids = itertools.count(1)


def tabs_for(key_id: int) -> list[Tab]:
    return list(_tabs.get(key_id, []))


def current_tab(key_id: int) -> Tab | None:
    tabs = _tabs.get(key_id) or []
    return max(tabs, key=lambda t: (t.active_at, t.connected_at)) if tabs else None


def status(key_id: int) -> dict[str, Any]:
    tabs = sorted(tabs_for(key_id), key=lambda t: (t.active_at, t.connected_at), reverse=True)
    return {"connected": bool(tabs), "tabs": [dict(t.public(), current=(i == 0)) for i, t in enumerate(tabs)]}


def reset_for_tests() -> None:
    _tabs.clear()
    _locks.clear()


class HgTransport:
    """BrowserTransport for one key: the call goes to that key's current tab, and only there."""

    def __init__(self, key_id: int) -> None:
        self.key_id = key_id

    async def call(self, name: str, args: dict[str, Any], timeout_ms: int) -> Any:
        if not name.startswith(PRIMITIVE_PREFIX):
            raise BrowserToolError("not_allowed", f"{name} is not available on hg")
        lock = _locks.setdefault(self.key_id, asyncio.Lock())
        async with lock:
            tab = current_tab(self.key_id)
            if tab is None:
                raise BrowserToolError(
                    "tab_offline",
                    "No StockTrak tab is connected for this key. Open app.stocktrak.com and click the "
                    "Hedgehog bookmarklet (hg.voitta.ai).")
            call_id = next(_ids)
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            tab.pending[call_id] = fut
            try:
                await tab.ws.send_text(json.dumps({"t": "call", "id": call_id, "name": name, "args": args}))
                return await asyncio.wait_for(fut, timeout=timeout_ms / 1000)
            except asyncio.TimeoutError:
                raise BrowserToolError("tab_unreachable",
                                       f"The StockTrak tab didn't answer {name} within {timeout_ms // 1000} s.") from None
            except BrowserToolError:
                raise
            except Exception as exc:  # socket gone mid-send
                raise BrowserToolError("tab_unreachable", f"The StockTrak tab went away ({exc}).") from None
            finally:
                tab.pending.pop(call_id, None)


def _drop(tab: Tab) -> None:
    tabs = _tabs.get(tab.key_id)
    if tabs and tab in tabs:
        tabs.remove(tab)
        if not tabs:
            _tabs.pop(tab.key_id, None)
    for fut in tab.pending.values():
        if not fut.done():
            fut.set_exception(BrowserToolError("tab_unreachable", "The StockTrak tab disconnected mid-call."))


async def retire(key_id: int) -> None:
    """The key was regenerated: unroute its tabs at once and tell them (the panel reconnects with
    the new key). Their sockets stay open until the panel closes them — no cross-task close."""
    for tab in tabs_for(key_id):
        _drop(tab)
        tab.retired = True
        try:
            await tab.ws.send_text(json.dumps({"t": "bye", "reason": "key_regenerated"}))
        except Exception:
            pass


def effective_origin(ws: WebSocket) -> str | None:
    """The page the panel runs in.

    Direct (regular bookmarklet): the browser's ``Origin`` header. Through the bridge window
    (Salesforce-style pages) the socket is opened by hg's OWN bridge page, so ``Origin`` is hg itself
    and the relay names the real page in ``?voitta_via=`` — learned from the opener's browser-verified
    postMessage origin. That tag is only believed when ``Origin`` is this hg host, so another site
    can't use it to pose as StockTrak."""
    origin = (ws.headers.get("origin") or "").strip().lower()
    host = (ws.headers.get("host") or "").strip().lower()
    own = {f"https://{host}", f"http://{host}"} if host else set()
    if origin in own:
        return ws.query_params.get("voitta_via")
    return origin or None


async def serve(ws: WebSocket) -> None:
    """One panel connection. Refuses anything not from a StockTrak page or without a live key."""
    if not origin_allowed(effective_origin(ws)):
        await ws.close(code=4403)
        return
    await ws.accept()
    try:
        hello = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=HELLO_TIMEOUT_S))
    except Exception:
        await ws.close(code=4400)
        return
    row = store.lookup(str(hello.get("key") or "")) if isinstance(hello, dict) and hello.get("t") == "hello" else None
    if row is None:
        await ws.send_text(json.dumps({"t": "bye", "reason": "bad_key"}))
        await ws.close(code=4401)
        return
    tab = Tab(key_id=row["id"], ws=ws, page=str(hello.get("page") or "")[:300])
    _tabs.setdefault(tab.key_id, []).append(tab)
    await ws.send_text(json.dumps({"t": "ready", "tab": tab.tid, "settings": store.public(row)}))
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            if not isinstance(msg, dict) or tab.retired:
                continue
            t = msg.get("t")
            if t == "active":
                tab.active_at = time.time()
                tab.page = str(msg.get("page") or tab.page)[:300]
            elif t in ("result", "error"):
                fut = tab.pending.get(msg.get("id"))
                if fut is None or fut.done():
                    continue
                if t == "result":
                    fut.set_result(msg.get("result"))
                else:
                    fut.set_exception(BrowserToolError(str(msg.get("kind") or "browser_error")[:40],
                                                       str(msg.get("message") or "")[:2000], msg.get("details")))
            elif t == "ping":
                await ws.send_text(json.dumps({"t": "pong"}))
    except (WebSocketDisconnect, RuntimeError, ValueError):
        pass
    finally:
        _drop(tab)
