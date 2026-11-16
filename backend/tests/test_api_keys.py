"""Per-user API keys for Claude Code over MCP (server mode): the store, the HTTP gates, the keys
page, and — through the real MCP protocol — per-key scopes and per-user tab isolation."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import cl_sessions, users

OWN = "http://testserver"
APPROVED = "https://app.stocktrak.com"
MCP_H = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VOITTA_GOOGLE_AUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("VOITTA_GOOGLE_AUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("VOITTA_PUBLIC_BASE_URL", "https://bookmarklet.voitta.ai")
    monkeypatch.setenv("VOITTA_OWNERS", "boss@x.com")
    monkeypatch.setenv("CHAINLIT_AUTH_SECRET", "test-secret-" + "x" * 40)
    users.invite(["ana@x.com", "bo@x.com"], password="pw")
    users.invite(["boss@x.com"], role="admin", password="boss-pw")


def _login(c: TestClient, email: str, pw: str) -> None:
    r = c.post("/api/auth/password", json={"email": email, "password": pw}, headers={"Origin": OWN})
    assert r.status_code == 200, r.text


# ── store ────────────────────────────────────────────────────────────────


def test_key_lifecycle(server):
    key, info = users.create_key("ana@x.com", "laptop")
    assert key.startswith("vbk_") and len(key) > 40
    assert info["prefix"] == key[:12] and info["scopes"] == ["agent"] and "key" not in info
    user, scopes = users.key_user(key)
    assert user["email"] == "ana@x.com" and scopes == ["agent"]
    assert users.key_user(key + "x") is None and users.key_user("nope") is None and users.key_user(None) is None
    assert users.list_keys("ana@x.com")[0]["last_used"] is not None
    assert users.revoke_key("bo@x.com", info["id"]) is False  # not bo's key
    assert users.revoke_key("ana@x.com", info["id"]) is True
    assert users.key_user(key) is None and users.list_keys("ana@x.com") == []


def test_disabled_user_keys_stop_and_scopes_are_validated(server):
    key, _ = users.create_key("ana@x.com", "k", ["agent", "debug", "bogus"])
    assert users.key_user(key)[1] == ["agent", "debug"]
    with pytest.raises(users.UserError):
        users.create_key("ana@x.com", "k", ["bogus"])
    users.disable("ana@x.com", actor="boss@x.com")
    assert users.key_user(key) is None
    users.enable("ana@x.com", actor="boss@x.com")
    assert users.key_user(key) is not None  # revoking is separate from disabling
    assert users.revoke_all_keys("ana@x.com", actor="boss@x.com") == 1 and users.key_user(key) is None


def test_admin_sees_counts_and_can_revoke(server):
    users.create_key("ana@x.com", "a")
    users.create_key("ana@x.com", "b")
    c = TestClient(app)
    _login(c, "boss@x.com", "boss-pw")
    rows = {u["email"]: u for u in c.get("/api/admin/users", headers={"Origin": OWN}).json()["users"]}
    assert rows["ana@x.com"]["api_keys"] == 2
    r = c.post("/api/admin/users/revoke-keys", json={"email": "ana@x.com"}, headers={"Origin": OWN})
    assert r.json()["revoked"] == 2 and users.list_keys("ana@x.com") == []


# ── keys API + page ──────────────────────────────────────────────────────


def test_create_key_only_from_our_own_page(server):
    c = TestClient(app)
    _login(c, "ana@x.com", "pw")
    users.approve_site("ana@x.com", APPROVED)
    # an approved site can act as ana while she's there, but must not mint a key that works anywhere
    r = c.post("/api/account/keys", json={"name": "x"}, headers={"Origin": APPROVED})
    assert r.status_code == 403
    r = c.post("/api/account/keys", json={"name": "laptop", "scopes": ["agent"]}, headers={"Origin": OWN})
    assert r.status_code == 200 and r.json()["key"].startswith("vbk_")
    listed = c.get("/api/account/keys", headers={"Origin": APPROVED}).json()["keys"]
    assert [k["name"] for k in listed] == ["laptop"] and "key" not in listed[0]
    assert c.get("/api/account", headers={"Origin": OWN}).json()["api_keys"] == 1
    kid = listed[0]["id"]
    assert c.delete(f"/api/account/keys/{kid}", headers={"Origin": OWN}).json()["keys"] == []
    page = c.get("/auth/keys")
    assert page.status_code == 200 and "frame-ancestors 'none'" in page.headers["content-security-policy"]


def test_keys_are_server_only(monkeypatch):
    monkeypatch.delenv("VOITTA_GOOGLE_AUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("VOITTA_GOOGLE_AUTH_CLIENT_SECRET", raising=False)
    c = TestClient(app)
    assert c.get("/api/account/keys").status_code == 404


# ── /mcp over the real MCP protocol ──────────────────────────────────────


def _rpc(c: TestClient, key: str | None, method: str, params: dict | None = None, *, origin: str | None = None):
    h = dict(MCP_H)
    if key:
        h["Authorization"] = f"Bearer {key}"
    if origin:
        h["Origin"] = origin
    r = c.post("/mcp/", headers=h, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
    if r.status_code != 200:
        return r.status_code, r.json()
    if r.headers.get("content-type", "").startswith("text/event-stream"):
        data = [ln[5:].strip() for ln in r.text.splitlines() if ln.startswith("data:")]
        return 200, json.loads(data[-1])
    return 200, r.json()


def _call(c, key, tool, **args):
    status, msg = _rpc(c, key, "tools/call", {"name": tool, "arguments": args})
    assert status == 200, msg
    res = msg["result"]
    if res.get("isError"):
        return {"tool_error": res["content"][0]["text"]}
    if res.get("structuredContent") is not None:
        sc = res["structuredContent"]
        return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
    return json.loads(res["content"][0]["text"])


def _fake_tabs(monkeypatch):
    """Two open widget tabs: sid-ana (ana's StockTrak) and sid-bo (bo's)."""
    owners = {"sid-ana": "ana@x.com", "sid-bo": "bo@x.com"}
    for sid, host in (("sid-ana", "app.stocktrak.com"), ("sid-bo", "secret.example")):
        cl_sessions.record_chat_start(sid)
        cl_sessions.record_host(sid, host)
    monkeypatch.setattr(cl_sessions, "_live_session_ids", lambda: set(owners))
    import chainlit.session as cs

    def get_by_id(sid):
        if sid in owners:
            return SimpleNamespace(id=sid, socket_id=sid, user=SimpleNamespace(identifier=owners[sid]))
        return None

    monkeypatch.setattr(cs.WebsocketSession, "get_by_id", staticmethod(get_by_id))
    return owners


def test_mcp_with_keys_end_to_end(server, monkeypatch):
    async def _noop(*a, **k):
        return None

    import app.services.mcp.registry as mcp_conn
    import app.services.models_catalog as models_catalog

    monkeypatch.setattr(mcp_conn, "refresh_all", _noop)
    monkeypatch.setattr(models_catalog, "warm_cached_credentialed", _noop)
    _fake_tabs(monkeypatch)
    agent_key, _ = users.create_key("ana@x.com", "agent only")
    full_key, _ = users.create_key("ana@x.com", "everything", ["agent", "debug", "chat"])

    with TestClient(app) as c:  # runs the lifespan: the MCP transport needs it
        # ── who may connect ──
        status, body = _rpc(c, None, "tools/list")
        assert status == 401 and body["error"] == "api_key_required"
        assert _rpc(c, "vbk_nope", "tools/list")[0] == 401
        assert _rpc(c, agent_key, "tools/list", origin="https://evil.example")[0] == 403

        status, init = _rpc(c, agent_key, "initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
        assert status == 200 and init["result"]["serverInfo"]["name"] == "voitta-bookmarklet"

        # ── scopes: an agent-only key sees and calls only the vb_* tools ──
        names = {t["name"] for t in _rpc(c, agent_key, "tools/list")[1]["result"]["tools"]}
        assert "vb_sessions" in names and "vb_call_tool" in names
        assert not any(n.startswith("mcp_") for n in names)
        assert "isn't allowed" in _call(c, agent_key, "mcp_sessions")["tool_error"]
        all_names = {t["name"] for t in _rpc(c, full_key, "tools/list")[1]["result"]["tools"]}
        assert {"mcp_sessions", "mcp_inject_text", "vb_sessions"} <= all_names

        # ── isolation: ana's key only ever sees and reaches ana's tab ──
        tabs = _call(c, agent_key, "vb_sessions")
        assert [t["session_id"] for t in tabs["sessions"]] == ["sid-ana"]
        assert tabs["sessions"][0]["active"] is True  # her own tab is the default, never bo's
        other = _call(c, agent_key, "vb_list_tools", session_id="sid-bo")
        assert other["ok"] is False and other["error"] == "no_session"
        assert "sid-bo" not in json.dumps(other.get("sessions", []))
        dbg = _call(c, full_key, "mcp_sessions")
        assert [s["session_id"] for s in dbg["sessions"]] == ["sid-ana"]
        for tool, args in (("mcp_page", {"session_id": "sid-bo"}),
                           ("mcp_inject_text", {"session_id": "sid-bo", "text": "hi"}),
                           ("mcp_eval", {"session_id": "sid-bo", "js": "1"})):
            res = _call(c, full_key, tool, **args)
            assert res.get("ok") is False and res.get("error") == "no_session", (tool, res)

        # control: the setup really has bo's tab live — only the owner filter hides it
        with monkeypatch.context() as m:
            m.setattr("app.services.mcp_auth.owner_filter", lambda: None)
            assert {t["session_id"] for t in _call(c, agent_key, "vb_sessions")["sessions"]} == {"sid-ana", "sid-bo"}

        # ── revoked / disabled ──
        kid = users.list_keys("ana@x.com")[0]["id"]
        users.revoke_key("ana@x.com", kid)
        users.revoke_key("ana@x.com", users.list_keys("ana@x.com")[0]["id"])
        assert _rpc(c, agent_key, "tools/list")[0] == 401
        assert _rpc(c, full_key, "tools/list")[0] == 401
