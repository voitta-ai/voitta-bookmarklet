"""Sign-in, sessions, site approval and the admin API, end to end through the ASGI app.

TestClient's host is ``testserver``, so our own origin here is ``http://testserver``.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.main import _AuthGuard, app
from app.services import login_auth, site_check, users

OWN = "http://testserver"
EVIL = "https://evil.example"
GOOD = "https://app.stocktrak.com"


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VOITTA_GOOGLE_AUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("VOITTA_GOOGLE_AUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("VOITTA_PUBLIC_BASE_URL", "https://bookmarklet.voitta.ai")
    monkeypatch.setenv("VOITTA_OWNERS", "boss@x.com")
    monkeypatch.setenv("CHAINLIT_AUTH_SECRET", "test-secret-" + "x" * 40)
    users.invite(["ana@x.com", "bo@x.com"], password="314159")
    users.invite(["boss@x.com"], role="admin", password="boss-pw")
    return TestClient(app)


@pytest.fixture
def desktop(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("VOITTA_GOOGLE_AUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("VOITTA_GOOGLE_AUTH_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("VOITTA_PUBLIC_BASE_URL", raising=False)
    return TestClient(app)


def _login(c: TestClient, email: str, pw: str) -> None:
    r = c.post("/api/auth/password", json={"email": email, "password": pw}, headers={"Origin": OWN})
    assert r.status_code == 200, r.text


# ── classify ─────────────────────────────────────────────────────────────


def _scope(headers: dict[str, str], *, query: str = "", scheme: str = "http", host: str = "testserver") -> dict:
    h = {"host": host, **headers}
    return {"type": "http", "scheme": scheme, "query_string": query.encode(),
            "headers": [(k.lower().encode(), v.encode()) for k, v in h.items()]}


@pytest.mark.parametrize("headers, query, want", [
    ({}, "", ("tool", None)),                                              # curl / MCP
    ({"Origin": OWN}, "", ("own", None)),
    ({"Sec-Fetch-Site": "same-origin"}, "", ("own", None)),
    ({"Sec-Fetch-Site": "none"}, "", ("own", None)),                       # typed URL
    ({"Origin": EVIL}, "", ("site", EVIL)),
    ({"Origin": "null"}, "", ("refuse", "opaque origin")),
    ({"Sec-Fetch-Site": "cross-site", "Referer": EVIL + "/page"}, "", ("site", EVIL)),   # <img src>
    ({"Sec-Fetch-Site": "cross-site"}, "", ("refuse", "cross-site request without origin")),
    ({"Origin": OWN, "X-Voitta-Via": EVIL}, "", ("site", EVIL)),           # bridge relay
    ({"Sec-Fetch-Site": "same-origin", "X-Voitta-Via": EVIL}, "", ("site", EVIL)),
    ({"Origin": OWN}, "voitta_via=" + EVIL, ("site", EVIL)),               # relayed WebSocket
    ({"Origin": EVIL, "X-Voitta-Via": GOOD}, "", ("site", EVIL)),          # a page can't claim another site
])
def test_classify(headers, query, want):
    assert site_check.classify(_scope(headers, query=query)) == want


def test_public_base_url_is_own(monkeypatch):
    monkeypatch.setenv("VOITTA_PUBLIC_BASE_URL", "https://bookmarklet.voitta.ai/")
    assert site_check.classify(_scope({"Origin": "https://bookmarklet.voitta.ai"}, host="10.0.0.5:12358")) == ("own", None)


# ── server: sessions ─────────────────────────────────────────────────────


def test_guarded_needs_sign_in(server):
    assert server.get("/api/account").status_code == 401
    _login(server, "ana@x.com", "314159")
    r = server.get("/api/account")
    assert r.status_code == 200 and r.json()["email"] == "ana@x.com"


def test_password_login_only_from_own_pages(server):
    r = server.post("/api/auth/password", json={"email": "ana@x.com", "password": "314159"}, headers={"Origin": EVIL})
    assert r.status_code == 403
    r = server.post("/api/auth/password", json={"email": "ana@x.com", "password": "314159"})  # no browser headers
    assert r.status_code == 403


def test_one_answer_for_wrong_password_and_unknown_email(server):
    a = server.post("/api/auth/password", json={"email": "ana@x.com", "password": "no"}, headers={"Origin": OWN})
    b = server.post("/api/auth/password", json={"email": "ghost@x.com", "password": "no"}, headers={"Origin": OWN})
    assert a.status_code == b.status_code == 401 and a.json() == b.json()


def test_disable_ends_the_open_session(server):
    _login(server, "ana@x.com", "314159")
    assert server.get("/api/account").status_code == 200
    users.disable("ana@x.com", actor="boss@x.com")
    assert server.get("/api/account").status_code == 401


def test_logout_revokes_session_server_side(server):
    _login(server, "ana@x.com", "314159")
    cookie = server.cookies.get("access_token")
    assert server.post("/api/auth/logout", headers={"Origin": OWN}).status_code == 200
    stale = TestClient(app)
    stale.cookies.set("access_token", cookie)
    assert stale.get("/api/account").status_code == 401  # a copied cookie dies too


def test_change_password_keeps_this_session_and_needs_current(server):
    _login(server, "ana@x.com", "314159")
    bad = server.post("/api/auth/change-password", json={"current_password": "x", "new_password": "n"},
                      headers={"Origin": OWN})
    assert bad.status_code == 401
    ok = server.post("/api/auth/change-password", json={"current_password": "314159", "new_password": "n2"},
                     headers={"Origin": OWN})
    assert ok.status_code == 200
    assert server.get("/api/account").status_code == 200
    assert users.password_login("ana@x.com", "n2")[0] is not None


# ── server: site approval ────────────────────────────────────────────────


def test_unapproved_site_refused_until_approved(server):
    _login(server, "ana@x.com", "314159")
    r = server.get("/api/account", headers={"Origin": GOOD})
    assert r.status_code == 403 and r.json() == {"error": "site_not_approved", "origin": GOOD}
    # another site can't approve itself
    assert server.post("/api/auth/sites/approve", json={"origin": GOOD}, headers={"Origin": GOOD}).status_code == 403
    assert server.post("/api/auth/sites/approve", json={"origin": GOOD}, headers={"Origin": OWN}).status_code == 200
    assert server.get("/api/account", headers={"Origin": GOOD}).status_code == 200
    # still refused for a different site, and for someone else's list
    assert server.get("/api/account", headers={"Origin": EVIL}).status_code == 403


def test_approval_is_per_user(server):
    _login(server, "ana@x.com", "314159")
    server.post("/api/auth/sites/approve", json={"origin": GOOD}, headers={"Origin": OWN})
    bo = TestClient(app)
    _login(bo, "bo@x.com", "314159")
    assert bo.get("/api/account", headers={"Origin": GOOD}).status_code == 403


def test_me_hides_email_from_unapproved_sites(server):
    _login(server, "ana@x.com", "314159")
    m = server.get("/api/auth/me", headers={"Origin": EVIL}).json()
    assert m["authenticated"] is True and m["email"] is None and m["site"] == {
        "origin": EVIL, "approved": False, "refused": False}
    m = server.get("/api/auth/me", headers={"Origin": OWN}).json()
    assert m["email"] == "ana@x.com" and m["role"] == "user" and m["has_password"] is True


def test_image_requests_judged_by_referer(server):
    _login(server, "ana@x.com", "314159")
    img = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Dest": "image"}
    assert server.get("/api/account", headers={**img, "Referer": GOOD + "/x"}).status_code == 403
    users.approve_site("ana@x.com", GOOD)
    assert server.get("/api/account", headers={**img, "Referer": GOOD + "/x"}).status_code == 200
    assert server.get("/api/account", headers=img).status_code == 403  # no-referrer trick


def test_bridge_relay_is_held_to_the_openers_approval(server):
    _login(server, "ana@x.com", "314159")
    relayed = {"Origin": OWN, "X-Voitta-Via": EVIL}
    assert server.get("/api/account", headers=relayed).status_code == 403
    users.approve_site("ana@x.com", EVIL)
    assert server.get("/api/account", headers=relayed).status_code == 200
    # and a relayed request can't approve sites (own pages only, never via the relay)
    assert server.post("/api/auth/sites/approve", json={"origin": "https://x.example"},
                       headers=relayed).status_code == 403


def test_remove_site(server):
    _login(server, "ana@x.com", "314159")
    users.approve_site("ana@x.com", GOOD)
    r = server.delete("/api/account/sites", params={"origin": GOOD}, headers={"Origin": OWN})
    assert r.status_code == 200 and r.json()["sites"] == []
    assert server.get("/api/account", headers={"Origin": GOOD}).status_code == 403


# ── desktop ──────────────────────────────────────────────────────────────


def test_desktop_refuses_unapproved_sites_and_foreign_hosts(desktop):
    assert desktop.get("/api/account").status_code == 200                       # MCP / curl / own pages
    assert desktop.get("/api/account", headers={"Origin": EVIL}).status_code == 403
    assert desktop.get("/api/account", headers={"Host": "evil.example:12358"}).status_code == 403  # rebinding
    assert desktop.post("/api/auth/sites/approve", json={"origin": GOOD},
                        headers={"Origin": OWN}).status_code == 200
    assert desktop.get("/api/account", headers={"Origin": GOOD}).status_code == 200
    me = desktop.get("/api/auth/me", headers={"Origin": EVIL}).json()
    assert me["enabled"] is False and me["site"]["approved"] is False


def test_websocket_handshake_is_guarded(desktop):
    sent: list[dict] = []

    async def inner(scope, receive, send):  # pragma: no cover - must not be reached
        raise AssertionError("guard let it through")

    async def send(msg):
        sent.append(msg)

    scope = _scope({"Origin": EVIL}) | {"type": "websocket", "path": "/chainlit/ws/socket.io/"}
    asyncio.run(_AuthGuard(inner)(scope, None, send))
    assert sent == [{"type": "websocket.close", "code": 4403}]


# ── Google ───────────────────────────────────────────────────────────────


def _claims(email: str, **kw) -> dict:
    import time

    return {"aud": "cid", "iss": "https://accounts.google.com", "exp": time.time() + 60, "sub": "sub-" + email,
            "email": email, "email_verified": True, **kw}


def test_check_claims(server):
    assert login_auth.check_claims(_claims("a@x.com")) is None
    assert login_auth.check_claims(_claims("a@x.com", aud="other")) == "issued for another app"
    assert login_auth.check_claims(_claims("a@x.com", iss="evil")) == "not issued by Google"
    assert login_auth.check_claims(_claims("a@x.com", exp=1)) == "expired"
    assert login_auth.check_claims(_claims("a@x.com", email_verified=False)) == "email not verified by Google"


def _google(c: TestClient, monkeypatch, claims: dict):
    monkeypatch.setenv("VOITTA_PUBLIC_BASE_URL", OWN)  # plain-http test client: state cookie not Secure
    start = c.get("/api/auth/google/start", follow_redirects=False)
    assert start.status_code == 303 and "code_challenge=" in start.headers["location"]
    state = start.headers["location"].split("state=")[1].split("&")[0]

    async def fake_exchange(code, verifier):
        assert verifier
        return claims

    monkeypatch.setattr(login_auth, "exchange_code", fake_exchange)
    return c.get("/api/auth/google/callback", params={"code": "c", "state": state})


def test_google_signs_in_a_password_user_too(server, monkeypatch):
    r = _google(server, monkeypatch, _claims("ana@x.com"))
    assert r.status_code == 200 and "Signed in" in r.text
    assert server.get("/api/account").json()["has_google"] is True
    assert r.headers["x-frame-options"] == "DENY"


def test_google_refuses_strangers(server, monkeypatch):
    r = _google(server, monkeypatch, _claims("stranger@x.com"))
    assert r.status_code == 403 and "Not invited" in r.text
    assert server.get("/api/account").status_code == 401


def test_google_state_must_come_from_this_browser(server, monkeypatch):
    start = server.get("/api/auth/google/start", follow_redirects=False)
    state = start.headers["location"].split("state=")[1].split("&")[0]
    other = TestClient(app)  # no state cookie
    r = other.get("/api/auth/google/callback", params={"code": "c", "state": state})
    assert r.status_code == 400 and "expired" in r.text


# ── admin ────────────────────────────────────────────────────────────────


def test_admin_only(server):
    _login(server, "ana@x.com", "314159")
    assert server.get("/api/admin/users", headers={"Origin": OWN}).status_code == 403


def test_admin_flow(server):
    _login(server, "boss@x.com", "boss-pw")
    h = {"Origin": OWN}
    r = server.post("/api/admin/users/invite", json={"emails": "new@x.com, bad", "role": "user",
                                                     "password": "314159"}, headers=h)
    assert [x["status"] for x in r.json()["results"]] == ["invited", "invalid"]
    gen = server.post("/api/admin/users/password", json={"email": "new@x.com", "mode": "generate"}, headers=h).json()
    assert len(gen["password"]) == 23 and users.password_login("new@x.com", gen["password"])[0]
    assert server.post("/api/admin/users/disable", json={"email": "new@x.com"}, headers=h).status_code == 200
    emails = [u["email"] for u in server.get("/api/admin/users", headers=h).json()["users"]]
    assert emails[0] == "boss@x.com" and emails[-1] == "new@x.com"  # owner first, disabled last
    # owner can't be disabled or demoted, nobody disables themselves
    assert server.post("/api/admin/users/disable", json={"email": "boss@x.com"}, headers=h).status_code == 400
    assert server.post("/api/admin/users/role", json={"email": "boss@x.com", "role": "user"}, headers=h).status_code == 400
    acts = [a["action"] for a in server.get("/api/admin/activity", headers=h).json()["activity"]]
    assert "user.disable" in acts and "user.invite" in acts


def test_admin_api_absent_on_desktop(desktop):
    assert desktop.get("/api/admin/users").status_code == 404


def test_pages_cannot_be_framed(server):
    for path in ("/auth/signin", "/auth/approve", "/auth/password"):
        r = server.get(path)
        assert r.status_code == 200
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


# ── chat side: session id comes from the socket's JWT, not the persisted user ──


def _chat_ctx(monkeypatch, token: str | None, persisted_meta: dict):
    """Fake what Chainlit hands a chat handler on saturn: the persisted user row
    (metadata from that person's first-ever sign-in) + the socket's raw JWT."""
    import chainlit as cl
    from chainlit.user import User

    from app import chainlit_app

    user = User(identifier="ana@x.com", metadata=persisted_meta)
    monkeypatch.setattr(cl.user_session, "get", lambda key, default=None: user if key == "user" else default)
    monkeypatch.setattr(chainlit_app, "_socket_token", lambda: token)


def test_chat_check_uses_the_socket_token_not_the_persisted_user(server, monkeypatch):
    from app import chainlit_app

    sid = users.new_session(users.get("ana@x.com"), "password")
    token = login_auth.mint_token("ana@x.com", sid, "password")
    _chat_ctx(monkeypatch, token, persisted_meta={})  # stored row has no sid — the saturn bug
    assert chainlit_app._apply_current_user() == "ana@x.com"

    users.revoke(sid)
    with pytest.raises(chainlit_app.SessionEnded):
        chainlit_app._apply_current_user()


def test_chat_check_ignores_a_stale_sid_in_persisted_metadata(server, monkeypatch):
    from app import chainlit_app

    old = users.new_session(users.get("ana@x.com"), "password")
    users.revoke(old)
    new = users.new_session(users.get("ana@x.com"), "google")
    _chat_ctx(monkeypatch, login_auth.mint_token("ana@x.com", new, "google"), persisted_meta={"sid": old})
    assert chainlit_app._apply_current_user() == "ana@x.com"
    _chat_ctx(monkeypatch, None, persisted_meta={"sid": new})  # no token → no session
    with pytest.raises(chainlit_app.SessionEnded):
        chainlit_app._apply_current_user()


def test_inline_allow_is_framable_but_clickjack_guarded(desktop):
    r = desktop.get("/auth/approve-embed")
    assert r.status_code == 200
    assert "x-frame-options" not in r.headers  # it must load inside the widget on any site
    assert "frame-ancestors *" in r.headers["content-security-policy"]
    page = r.text
    # its safety comes from these: visibility tracking, a 1 s arm delay, a minimum size,
    # the site taken from the browser (ancestorOrigins), and no opacity on observed elements
    for must in ("trackVisibility: true", "ARM_MS = 1000", "MIN_W", "location.ancestorOrigins", "ev.isTrusted"):
        assert must in page, must
    assert "button:disabled{opacity" not in page.replace(" ", "")
    # the popup version stays unframable
    assert desktop.get("/auth/approve").headers["x-frame-options"] == "DENY"


def test_bridge_window_cannot_be_framed_and_asks_before_boot(desktop):
    r = desktop.get("/bridge")
    assert r.status_code == 200
    assert r.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    relay = desktop.get("/bridge/relay.js").text
    # the approval check runs before the assistant is sent to the page
    assert "approveThenBoot" in relay and "/api/auth/sites/status" in relay
    assert relay.index("approveThenBoot()") < relay.index("function sendBoot")
