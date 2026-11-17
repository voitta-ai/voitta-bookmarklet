"""hg.voitta.ai: the fence, keys, the tab link, REST and MCP.

Most important: nothing on the main app is reachable through the hg host, and hg keys only ever
reach their own StockTrak tab."""

from __future__ import annotations

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.hg import link, runner, store
from app.main import app

HG = "https://hg.voitta.ai"
ST = "https://app.stocktrak.com"


@pytest.fixture
def hg():
    with TestClient(app, base_url=HG) as c:
        yield c


def _key(c: TestClient) -> str:
    r = c.post("/hg/keys")
    assert r.status_code == 200, r.text
    return r.json()["key"]


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


# ── the fence ────────────────────────────────────────────────────────────

MAIN_PATHS = [
    ("GET", "/api/settings"), ("PUT", "/api/settings"), ("GET", "/api/admin/users"),
    ("POST", "/api/admin/users/invite"), ("GET", "/api/account"), ("GET", "/api/plugins"),
    ("GET", "/api/workspace/files"), ("POST", "/api/workspace/scripts/x/run"), ("GET", "/api/auth/me"),
    ("POST", "/api/auth/password"), ("GET", "/api/plugin-events/hedgehog"), ("GET", "/api/models/anthropic"),
    ("GET", "/chainlit/project/settings"), ("GET", "/chainlit/ws/socket.io/?EIO=4&transport=polling"),
    ("GET", "/widget.js"), ("GET", "/bookmarklets"), ("GET", "/auth/signin"),
    ("GET", "/auth/keys"), ("GET", "/health"), ("GET", "/api/eval/v1/sessions"),
]


@pytest.mark.parametrize("method,path", MAIN_PATHS)
def test_main_app_is_unreachable_through_hg(hg, method, path):
    key = _key(hg)
    r = hg.request(method, path, headers={**_auth(key), "Origin": ST})
    assert r.status_code in (404, 405), (path, r.status_code, r.text[:200])
    assert "error" in r.json()  # the hg app's own 404, never a main-app body


def test_main_mcp_tools_are_not_on_hg(hg):
    key = _key(hg)
    r = hg.post("/mcp/", headers={**_auth(key), "Accept": "application/json, text/event-stream",
                                  "Content-Type": "application/json"},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    names = {t["name"] for t in _sse(r)["result"]["tools"]}
    assert names and all(n.startswith("hedgehog_") for n in names)
    assert not {"vb_call_tool", "mcp_eval", "mcp_inject_text", "hedgehog_export_csv", "hedgehog_session_log"} & names


def test_hg_ignores_cookies_and_account_keys(hg, monkeypatch):
    from app.services import users

    monkeypatch.setenv("VOITTA_GOOGLE_AUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("VOITTA_GOOGLE_AUTH_CLIENT_SECRET", "cs")
    users.invite(["ana@x.com"], password="pw")
    account_key, _ = users.create_key("ana@x.com", "k", ["agent", "debug", "chat"])
    hg.cookies.set("access_token", "anything")
    assert hg.get("/v1/account").status_code == 401  # a cookie is never an identity here
    assert hg.get("/v1/account", headers=_auth(account_key)).status_code == 401  # nor is an account key
    r = hg.post("/mcp/", headers={**_auth(account_key), "Accept": "application/json, text/event-stream",
                                  "Content-Type": "application/json"},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert r.status_code == 401


def test_hg_api_is_not_on_the_main_host():
    with TestClient(app) as c:
        assert c.post("/hg/keys").status_code in (404, 405)
        r = c.get("/v1/account")
        assert "application/json" not in r.headers.get("content-type", "")  # main app's page fallback, not hg


def test_openapi_lists_only_the_hg_api(hg):
    paths = set(hg.get("/openapi.json").json()["paths"])
    assert "/v1/orders" in paths and "/v1/account" in paths
    assert not [p for p in paths if not p.startswith("/v1/")]


# ── keys ─────────────────────────────────────────────────────────────────


def test_mint_regenerate_settings(hg):
    key = _key(hg)
    me = hg.get("/hg/me", headers=_auth(key)).json()
    assert me["trading"] is True and me["keepalive"] is True and me["tab"]["connected"] is False
    r = hg.patch("/hg/settings", headers=_auth(key), json={"trading": False})
    assert r.json()["trading"] is False
    new = hg.post("/hg/keys/regenerate", headers=_auth(key)).json()
    assert new["key"] != key and new["trading"] is False  # settings carry over
    assert hg.get("/hg/me", headers=_auth(key)).status_code == 401  # old key dead at once
    assert hg.get("/hg/me", headers=_auth(new["key"])).status_code == 200


def test_mint_is_rate_limited_per_ip(hg, monkeypatch):
    monkeypatch.setattr(store, "MINT_PER_IP_PER_HOUR", 3)
    codes = [hg.post("/hg/keys", headers={"X-Forwarded-For": "9.9.9.9"}).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    assert hg.post("/hg/keys", headers={"X-Forwarded-For": "8.8.8.8"}).status_code == 200


def test_bad_keys(hg):
    for k in (None, "hgk_nope", "vbk_x", "x" * 500):
        assert hg.get("/v1/status", headers=_auth(k) if k else {}).status_code == 401


# ── the tab link ─────────────────────────────────────────────────────────


class FakePanel:
    """A StockTrak tab's panel over the real /hg/link WebSocket, answering primitive calls."""

    def __init__(self, client: TestClient, key: str, page: str = f"{ST}/dashboard", origin: str = ST):
        self.ws = client.websocket_connect("wss://hg.voitta.ai/hg/link", headers={"Origin": origin}).__enter__()
        self.ws.send_text(json.dumps({"t": "hello", "key": key, "page": page}))
        self.ready = json.loads(self.ws.receive_text())
        self.calls: list[dict] = []

    def answer_one(self, reply=lambda call: {"t": "result", "result": {"kind": "json", "body": {"ok": 1}}}):
        call = json.loads(self.ws.receive_text())
        self.calls.append(call)
        self.ws.send_text(json.dumps({**reply(call), "id": call["id"]}))
        return call

    def active(self):
        self.ws.send_text(json.dumps({"t": "active"}))

    def close(self):
        self.ws.__exit__(None, None, None)


@pytest.fixture
def panels(hg):
    """Open fake tabs; ALWAYS closed after the test (an open test WebSocket blocks shutdown)."""
    opened: list[FakePanel] = []

    def make(key: str, **kw) -> FakePanel:
        p = FakePanel(hg, key, **kw)
        opened.append(p)
        return p

    yield make
    for p in opened:
        try:
            p.close()
        except Exception:
            pass


QUOTE_OK = {"t": "result", "result": {"kind": "json", "status": 200,
                                       "body": {"Success": True, "Symbol": "F", "LastPrice": "12.30"}}}


def test_link_refuses_other_sites_and_bad_keys(hg, panels):
    key = _key(hg)
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with hg.websocket_connect("wss://hg.voitta.ai/hg/link", headers={"Origin": "https://evil.example"}) as ws:
            ws.receive_text()
    with hg.websocket_connect("wss://hg.voitta.ai/hg/link", headers={"Origin": ST}) as ws:
        ws.send_text(json.dumps({"t": "hello", "key": "hgk_wrong"}))
        assert json.loads(ws.receive_text()) == {"t": "bye", "reason": "bad_key"}
    p = panels(key)
    assert p.ready["t"] == "ready" and p.ready["settings"]["trading"] is True
    assert hg.get("/v1/status?check=false", headers=_auth(key)).json()["tab"]["connected"] is True


def _in_thread(fn):
    out: dict = {}
    t = threading.Thread(target=lambda: out.update(r=fn()), daemon=True)
    t.start()
    return t, out


def test_calls_reach_only_the_keys_own_tab(hg, panels):
    ka, kb = _key(hg), _key(hg)
    pa, pb = panels(ka), panels(kb)
    t, out = _in_thread(lambda: hg.get("/v1/quotes/F", headers=_auth(ka)))
    call = pa.answer_one(lambda c: QUOTE_OK)
    t.join(10)
    assert call["name"] == "hedgehog_request" and call["args"]["path"] == "/trading/getpricev2"
    assert out["r"].status_code == 200, out["r"].text
    assert pb.calls == []  # b's tab never saw a's call
    assert [a["op"] for a in hg.get("/hg/activity", headers=_auth(ka)).json()["activity"]] == ["quote"]
    assert hg.get("/hg/activity", headers=_auth(kb)).json()["activity"] == []


def test_most_recently_active_tab_wins(hg, panels):
    key = _key(hg)
    first, second = panels(key), panels(key)
    first.active()
    time.sleep(0.05)
    t, _ = _in_thread(lambda: hg.get("/v1/quotes/F", headers=_auth(key)))
    first.answer_one(lambda c: QUOTE_OK)
    t.join(10)
    st = hg.get("/v1/status?check=false", headers=_auth(key)).json()["tab"]
    assert len(st["tabs"]) == 2 and [x["current"] for x in st["tabs"]] == [True, False]
    second.active()
    time.sleep(0.05)
    t, _ = _in_thread(lambda: hg.get("/v1/quotes/F", headers=_auth(key)))
    second.answer_one(lambda c: QUOTE_OK)
    t.join(10)
    assert len(first.calls) == 1 and len(second.calls) == 1


def test_page_errors_come_back_in_the_envelope(hg, panels):
    key = _key(hg)
    p = panels(key)
    t, out = _in_thread(lambda: hg.get("/v1/quotes/F", headers=_auth(key)))  # one tab call
    p.answer_one(lambda c: {"t": "error", "kind": "session_expired", "message": "redirected to /login"})
    t.join(10)
    r = out["r"]
    assert r.status_code == 409 and r.json()["error"]["code"] == "session_expired"
    assert r.json()["error"]["retryable"] is False


def test_regenerate_retires_tabs_on_the_old_key(hg, panels):
    key = _key(hg)
    p = panels(key)
    old_id = store.lookup(key)["id"]
    assert link.status(old_id)["connected"] is True
    new = hg.post("/hg/keys/regenerate", headers=_auth(key)).json()["key"]
    assert link.status(old_id)["connected"] is False  # unrouted at once
    assert json.loads(p.ws.receive_text()) == {"t": "bye", "reason": "key_regenerated"}
    # the old tab gets no calls for the new key: with no tab on the new key, calls fail fast
    r = hg.get("/v1/quotes/F", headers=_auth(new))
    assert r.json()["error"]["code"] == "tab_offline"


def test_transport_only_carries_hedgehog_primitives():
    import asyncio

    from app.tools.browser import BrowserToolError

    with pytest.raises(BrowserToolError) as e:
        asyncio.run(link.HgTransport(1).call("eval_js", {"code": "1"}, 1000))
    assert e.value.kind == "not_allowed"


# ── trading switch, settings isolation ───────────────────────────────────


def test_trading_switch_off_blocks_orders(hg):
    key = _key(hg)
    hg.patch("/hg/settings", headers=_auth(key), json={"trading": False})
    r = hg.post("/v1/orders", headers=_auth(key), json={"symbol": "F", "side": "buy", "quantity": 1,
                                                         "trade_note": "test"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "trading_disabled"


def test_trade_note_is_required(hg):
    key = _key(hg)
    r = hg.post("/v1/orders", headers=_auth(key), json={"symbol": "F", "side": "buy", "quantity": 1})
    assert r.status_code == 422


def test_hg_calls_never_read_the_servers_settings_file(hg, monkeypatch):
    from app.services import user_settings

    def boom():
        raise AssertionError("hg read the shared settings file")

    monkeypatch.setattr(user_settings, "read", boom)
    key = _key(hg)
    r = hg.post("/v1/orders", headers=_auth(key), json={"symbol": "F", "side": "buy", "quantity": 1,
                                                         "trade_note": "test"})
    assert r.json()["error"]["code"] == "tab_offline"  # got as far as the tab, settings came from the key


def test_runner_excludes_server_storage_tools():
    names = runner.tool_names()
    assert "hedgehog_account" in names and "hedgehog_place_order" in names
    assert "hedgehog_export_csv" not in names and "hedgehog_session_log" not in names


# ── MCP ──────────────────────────────────────────────────────────────────

_MCP_H = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _sse(r) -> dict:
    if r.headers.get("content-type", "").startswith("text/event-stream"):
        return json.loads([ln[5:] for ln in r.text.splitlines() if ln.startswith("data:")][-1])
    return r.json()


def test_mcp_auth_and_tools(hg):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    assert hg.post("/mcp/", headers=_MCP_H, json=body).status_code == 401
    key = _key(hg)
    assert hg.post("/mcp/", headers={**_MCP_H, **_auth(key), "Origin": "https://evil.example"}, json=body).status_code == 403
    r = hg.post("/mcp/", headers={**_MCP_H, **_auth(key)}, json=body)
    tools = {t["name"]: t for t in _sse(r)["result"]["tools"]}
    assert set(tools) == set(runner.tool_names())
    assert "trade_note" in tools["hedgehog_preview_order"]["inputSchema"]["required"]
    r = hg.post("/mcp/", headers={**_MCP_H, **_auth(key)},
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "hedgehog_account", "arguments": {}}})
    res = _sse(r)["result"]
    assert res["isError"] is True and "tab_offline" in res["content"][0]["text"]


# ── the Salesforce (bridge) bookmarklet ────────────────────────────────────


def test_bridge_link_trusts_via_only_from_our_own_bridge(hg, panels):
    key = _key(hg)
    from starlette.websockets import WebSocketDisconnect

    def connect(origin, via):
        url = f"wss://hg.voitta.ai/hg/link?voitta_via={via}"
        with hg.websocket_connect(url, headers={"Origin": origin}) as ws:
            ws.send_text(json.dumps({"t": "hello", "key": key}))
            return json.loads(ws.receive_text())["t"]

    # through hg's own bridge window (Origin = hg) naming StockTrak → accepted
    assert connect("https://hg.voitta.ai", ST) == "ready"
    # another site can't claim to be StockTrak with the tag
    with pytest.raises(WebSocketDisconnect):
        connect("https://evil.example", ST)
    # our bridge relaying for a non-StockTrak page → refused
    with pytest.raises(WebSocketDisconnect):
        connect("https://hg.voitta.ai", "https://evil.example")


def test_landing_has_both_bookmarklets_draggable_and_copyable(hg):
    import html as _html
    import re as _re

    page = hg.get("/").text
    links = dict(_re.findall(r'<a id=(bm-\w+)[^>]*href="(javascript:[^"]+)"', page))
    assert set(links) == {"bm-regular", "bm-enhanced"}
    for bid in links:
        assert f"data-copy={bid}" in page
    assert "/hg/panel.js" in _html.unescape(links["bm-regular"])
    assert "/bridge'" in _html.unescape(links["bm-enhanced"])


def test_hg_bridge_serves_the_hg_panel_without_site_approval(hg):
    relay = hg.get("/bridge/relay.js").text
    boot = hg.get("/bridge/boot.js").text
    assert "approveThenBoot();\n      return;" not in relay  # no account → no approval step
    assert "/hg/panel.js" in boot and 'BACKEND + "/widget.js"' not in boot
    assert hg.get("/bridge").headers["x-frame-options"] == "DENY"


def test_hg_bridge_is_hg_only_not_the_main_one(hg):
    # /bridge exists on hg, but it's hg's own: titled Hedgehog, boots the hg panel, never the main
    # widget, and has no approve/sign-in calls into the main app.
    page = hg.get("/bridge").text
    assert "Hedgehog bridge" in page and "Voitta bridge" not in page
    relay = hg.get("/bridge/relay.js").text
    assert "approveThenBoot();\n      return;" not in relay
    assert 'BACKEND + "/widget.js"' not in hg.get("/bridge/boot.js").text


# ── Claude Desktop extension (.mcpb) ─────────────────────────────────────


def _mcpb(hg, key):
    import io
    import zipfile

    r = hg.post("/hg/claude-desktop.mcpb", headers=_auth(key))
    assert r.status_code == 200, r.text
    assert 'filename="Hedgehog.mcpb"' in r.headers["content-disposition"]
    z = zipfile.ZipFile(io.BytesIO(r.content))
    return z, json.loads(z.read("manifest.json"))


def test_mcpb_needs_the_key_in_a_header(hg):
    assert hg.post("/hg/claude-desktop.mcpb").status_code == 401
    assert hg.get("/hg/claude-desktop.mcpb").status_code == 405  # never a GET link with the key in it


def test_mcpb_manifest_follows_the_spec(hg):
    key = _key(hg)
    z, m = _mcpb(hg, key)
    assert set(z.namelist()) == {"manifest.json", "server/index.js"}
    for field in ("manifest_version", "name", "version", "description", "author", "server"):
        assert field in m, field
    assert m["manifest_version"] == "0.3" and m["author"]["name"]
    s = m["server"]
    assert s["type"] == "node" and s["entry_point"] == "server/index.js"
    assert s["mcp_config"]["command"] == "node" and s["mcp_config"]["args"] == ["${__dirname}/server/index.js"]
    assert s["mcp_config"]["env"] == {"HG_URL": "https://hg.voitta.ai/mcp/", "HG_KEY": key}


def test_a_new_download_replaces_the_installed_one(hg):
    """Same name, higher version: Claude Desktop upgrades in place, so the new key replaces the old."""
    key = _key(hg)
    _, first = _mcpb(hg, key)
    time.sleep(1.1)
    new = hg.post("/hg/keys/regenerate", headers=_auth(key)).json()["key"]
    _, second = _mcpb(hg, new)
    assert first["name"] == second["name"] == "hedgehog"

    def semver(v):
        return tuple(int(x) for x in v.split("."))

    assert semver(second["version"]) > semver(first["version"])
    assert second["server"]["mcp_config"]["env"]["HG_KEY"] == new
    assert hg.post("/hg/claude-desktop.mcpb", headers=_auth(key)).status_code == 401  # old key is dead


def test_mcpb_bridge_runs_like_claude_desktop(tmp_path):
    """Unpack the real .mcpb and run its server/index.js with Node, speaking MCP over stdio, against a
    live hg server on a free port — the way Claude Desktop runs it."""
    import io
    import shutil
    import socket
    import subprocess
    import zipfile

    import uvicorn

    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")

    port = socket.socket()
    port.bind(("127.0.0.1", 0))
    p = port.getsockname()[1]
    port.close()
    # The real app (its startup enters the hg MCP transport); an hg Host reaches the hg app.
    import os

    os.environ["VOITTA_HG_HOSTS"] = "127.0.0.1"
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=p, log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    try:
        base = f"http://127.0.0.1:{p}"
        key = store.mint("127.0.0.1")[0]
        from app.hg import claude_desktop

        data, _ = claude_desktop.build(key, f"{base}/mcp/")
        zipfile.ZipFile(io.BytesIO(data)).extractall(tmp_path)
        m = json.loads((tmp_path / "manifest.json").read_text())
        cfg = m["server"]["mcp_config"]
        args = [a.replace("${__dirname}", str(tmp_path)) for a in cfg["args"]]
        proc = subprocess.Popen([node, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env={"PATH": "/usr/bin:/bin", **cfg["env"]})

        def rpc(i, method, params=None):
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}}) + "\n")
            proc.stdin.flush()
            return json.loads(proc.stdout.readline())

        init = rpc(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                      "clientInfo": {"name": "claude-desktop-sim", "version": "0"}})
        assert init["result"]["serverInfo"]["name"] == "hedgehog"
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        proc.stdin.flush()
        tools = rpc(2, "tools/list")["result"]["tools"]
        assert {t["name"] for t in tools} == set(runner.tool_names())
        res = rpc(3, "tools/call", {"name": "hedgehog_account", "arguments": {}})["result"]
        assert res["isError"] is True and "tab_offline" in res["content"][0]["text"]  # no tab here: honest error

        # a regenerated key: the old extension reports it clearly instead of hanging
        store.regenerate(key)
        err = rpc(4, "tools/list")
        assert "regenerated or revoked" in err["error"]["message"]
        proc.stdin.close()
        proc.wait(10)
    finally:
        server.should_exit = True
        th.join(10)
        os.environ.pop("VOITTA_HG_HOSTS", None)