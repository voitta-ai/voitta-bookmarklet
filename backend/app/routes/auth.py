"""Sign-in and site-approval routes. See app.services.login_auth, users, site_check.

Everything under /api/auth/* is left open by the main guard so sign-in can run before a session
exists; each route here checks what it needs itself. Routes that change who you are or what you
allow (password sign-in, change password, approve a site) only accept requests from our own pages
(``site_check.is_own``), so another site can't drive them.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.routes import auth_pages
from app.services import login_auth, passwords, site_check, users

router = APIRouter(prefix="/api/auth")


def client_ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()[:64]
    return request.client.host if request.client else None


def _set_session_cookie(request: Request, response, email: str, sid: str, method: str) -> None:
    """Set the single identity cookie (Chainlit's access_token JWT) with Chainlit's helper
    (honours SameSite=None + the shared secret, handles chunking)."""
    from chainlit.auth.cookie import set_auth_cookie

    set_auth_cookie(request, response, login_auth.mint_token(email, sid, method))


def _start_session(request: Request, response, u: dict, method: str) -> None:
    sid = users.new_session(u, method, ip=client_ip(request))
    _set_session_cookie(request, response, u["email"], sid, method)


def _own_only(request: Request) -> JSONResponse | None:
    if site_check.is_own(request.scope):
        return None
    return JSONResponse({"error": "own_pages_only", "detail": "Use the Voitta page for this."}, status_code=403)


def current(request: Request) -> tuple[str | None, str | None]:
    """Live ``(email, sid)`` of the caller in server mode."""
    return login_auth.live_email_from_cookie_header(request.headers.get("cookie"))


def site_owner(request: Request) -> str | None:
    """Whose approved-site list applies: the signed-in email (server), ``local`` (desktop)."""
    if not login_auth.is_enabled():
        return users.LOCAL_OWNER
    return current(request)[0]


# ── state probe ──────────────────────────────────────────────────────────


@router.get("/me")
async def me(request: Request) -> dict:
    """Always 200 — the widget's open probe. Reveals who you are only to our own pages and to
    sites you've approved, so an unknown page learns nothing beyond "signed in / not"."""
    enabled = login_auth.is_enabled()
    kind, origin = site_check.classify(request.scope)
    email = current(request)[0] if enabled else None
    owner = email if enabled else users.LOCAL_OWNER
    if kind in ("own", "tool"):
        approved = True
    elif kind == "site" and owner:
        approved = users.site_approved(owner, origin)
    else:
        approved = False
    out: dict = {
        "enabled": enabled,
        "authenticated": bool(email),
        "email": None,
        "site": {"origin": origin if kind == "site" else None, "approved": approved, "refused": kind == "refuse"},
    }
    if email and approved:
        u = users.get(email)
        out.update(email=email, role=users.role_of(u) if u else "user", is_admin=users.is_admin(u),
                   has_password=bool(u and u.get("password_hash")), has_google=bool(u and u.get("google_sub")))
    return out


# ── Google ───────────────────────────────────────────────────────────────


@router.get("/google/start")
async def google_start():
    if not login_auth.is_enabled():
        return JSONResponse({"error": "not_configured"}, status_code=404)
    url, state = login_auth.build_authorize_url()
    resp = RedirectResponse(url, status_code=303)
    resp.set_cookie(login_auth.STATE_COOKIE, state, max_age=login_auth.STATE_TTL_S, httponly=True,
                    secure=login_auth.redirect_uri().startswith("https://"), samesite="lax", path="/api/auth")
    return resp


@router.get("/google/callback")
async def google_callback(request: Request, code: str | None = None, state: str | None = None,
                          error: str | None = None):
    if not login_auth.is_enabled():
        return JSONResponse({"error": "not_configured"}, status_code=404)
    if error:
        msg = "Google sign-in was cancelled." if error == "access_denied" else f"Google said: {error}"
        return auth_pages.message("Sign-in cancelled", msg, status=400)
    verifier = login_auth.consume_state(state or "", request.cookies.get(login_auth.STATE_COOKIE))
    if not code or verifier is None:
        return auth_pages.message("Sign-in expired",
                                  "This sign-in link has expired or was opened in another browser. Start again.",
                                  status=400)
    try:
        claims = await login_auth.exchange_code(code, verifier)
    except Exception:
        return auth_pages.message("Sign-in failed", "Could not reach Google to finish signing in. Try again.",
                                  status=502)
    bad = login_auth.check_claims(claims)
    if bad:
        return auth_pages.message("Sign-in failed", f"Google's answer was not acceptable ({bad}).", status=400)
    u, why = users.admit_google(str(claims["email"]), str(claims["sub"]), ip=client_ip(request))
    if u is None:
        return auth_pages.refused(str(claims.get("email") or ""), why)
    resp = auth_pages.signed_in(u["email"])
    _start_session(request, resp, u, "google")
    resp.delete_cookie(login_auth.STATE_COOKIE, path="/api/auth")
    return resp


# ── password ─────────────────────────────────────────────────────────────


async def _body(request: Request) -> dict:
    try:
        b = await request.json()
    except Exception:
        return {}
    return b if isinstance(b, dict) else {}


@router.post("/password")
async def password_login(request: Request):
    if not login_auth.is_enabled():
        return JSONResponse({"error": "not_configured"}, status_code=404)
    if (bad := _own_only(request)) is not None:
        return bad
    b = await _body(request)
    email = str(b.get("email") or "")[:254]
    u, why = users.password_login(email, str(b.get("password") or ""), ip=client_ip(request))
    if u is None:
        if why == "locked":
            mins = users.lock_minutes(email)
            return JSONResponse({"detail": f"Too many wrong passwords. Try again in {mins} min, or continue "
                                           "with Google."}, status_code=429)
        if why == "disabled":
            return JSONResponse({"detail": "This account is disabled. Ask your admin."}, status_code=403)
        return JSONResponse({"detail": "Wrong email or password."}, status_code=401)
    resp = JSONResponse({"ok": True, "email": u["email"]})
    _start_session(request, resp, u, "password")
    return resp


@router.post("/change-password")
async def change_password(request: Request):
    if (bad := _own_only(request)) is not None:
        return bad
    email, sid = current(request)
    if not email:
        return JSONResponse({"detail": "Sign in first."}, status_code=401)
    b = await _body(request)
    new = str(b.get("new_password") or "")
    if (problem := passwords.problem(new)) is not None:
        return JSONResponse({"detail": problem}, status_code=400)
    u = users.get(email)
    if u and u.get("password_hash") and not passwords.verify(str(b.get("current_password") or ""),
                                                             u["password_hash"]):
        users.log("user.password_change", actor=email, target=email, ok=False, ip=client_ip(request))
        return JSONResponse({"detail": "Current password is wrong."}, status_code=401)
    users.set_password(email, new, actor=email, keep_sid=sid, ip=client_ip(request))
    return {"ok": True}


@router.post("/logout")
async def logout(request: Request):
    from chainlit.auth.cookie import clear_auth_cookie

    kind, origin = site_check.classify(request.scope)
    email, sid = current(request)
    if kind == "site" and not (email and users.site_approved(email, origin)):
        return JSONResponse({"error": "site_not_approved"}, status_code=403)
    if kind == "refuse":
        return JSONResponse({"error": "site_refused"}, status_code=403)
    users.revoke(sid)
    resp = JSONResponse({"ok": True})
    clear_auth_cookie(request, resp)
    return resp


# ── site approval ────────────────────────────────────────────────────────


@router.post("/sites/approve")
async def approve_site(request: Request):
    """Add a site to the caller's approved list. Own pages only — the approve popup, which
    learns the site from the opener's browser-verified postMessage origin."""
    if (bad := _own_only(request)) is not None:
        return bad
    owner = site_owner(request)
    if not owner:
        return JSONResponse({"detail": "Sign in first."}, status_code=401)
    b = await _body(request)
    try:
        origin = users.approve_site(owner, str(b.get("origin") or ""), ip=client_ip(request))
    except users.UserError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    return {"ok": True, "origin": origin}


@router.get("/sites/status")
async def site_status(request: Request, origin: str) -> dict:
    """For the approve page (own origin): is ``origin`` already approved, and who am I?"""
    if (bad := _own_only(request)) is not None:
        return bad  # type: ignore[return-value]
    owner = site_owner(request)
    o = users.normalize_origin(origin)
    return {
        "signed_in": bool(owner),
        "email": owner if login_auth.is_enabled() else None,
        "origin": o,
        "approved": bool(owner and o and users.site_approved(owner, o)),
    }
