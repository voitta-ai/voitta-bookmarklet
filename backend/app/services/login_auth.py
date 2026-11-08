"""Server-mode sign-in: Google and email + password, invite only.

Who may get in is decided by :mod:`app.services.users` (invited emails + ``VOITTA_OWNERS``).
This module does the Google OAuth flow and the session token.

Identity has one source of truth: the Chainlit JWT (``access_token`` cookie). It carries the email
as the user identifier and the session id (``sid``) in its metadata. Our HTTP guard
(app.main._AuthGuard) verifies it and re-checks the session in users.sqlite on every request;
Chainlit verifies the same cookie for /chainlit, so conversation isolation stays native.

Activation is env-driven: sign-in is ON iff ``VOITTA_GOOGLE_AUTH_CLIENT_ID`` and
``VOITTA_GOOGLE_AUTH_CLIENT_SECRET`` are set (a server .env; never the macOS bundle). The redirect
URI is ``{VOITTA_PUBLIC_BASE_URL}/api/auth/google/callback``.

Google flow (as in voitta-robot-war): PKCE (S256), a state value bound to the browser by a
short-lived cookie, and the ID token's aud / iss / exp / email_verified checked before the email is
trusted. The token comes straight from Google's token endpoint over TLS, so its signature isn't
re-verified.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from http.cookies import SimpleCookie
from urllib.parse import urlencode

import httpx

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = ["openid", "email"]
ISSUERS = {"accounts.google.com", "https://accounts.google.com"}
STATE_COOKIE = "voitta_oauth_state"
STATE_TTL_S = 10 * 60


def is_enabled() -> bool:
    """True iff sign-in is active (server mode)."""
    return bool(os.getenv("VOITTA_GOOGLE_AUTH_CLIENT_ID") and os.getenv("VOITTA_GOOGLE_AUTH_CLIENT_SECRET"))


def _client() -> tuple[str, str]:
    cid = os.getenv("VOITTA_GOOGLE_AUTH_CLIENT_ID")
    csec = os.getenv("VOITTA_GOOGLE_AUTH_CLIENT_SECRET")
    if not cid or not csec:
        raise RuntimeError("login not configured (missing client id/secret)")
    return cid, csec


def redirect_uri() -> str:
    base = (os.getenv("VOITTA_PUBLIC_BASE_URL") or "").rstrip("/")
    return f"{base}/api/auth/google/callback"


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ── session token (Chainlit JWT) ─────────────────────────────────────────


def mint_token(email: str, sid: str, method: str) -> str:
    from chainlit.auth.jwt import create_jwt
    from chainlit.user import User

    return create_jwt(User(identifier=email, metadata={"sid": sid, "m": method}))


def identity_from_cookie_header(cookie_header: str | None) -> tuple[str | None, str | None]:
    """``(email, sid)`` from a verified Chainlit ``access_token`` JWT in a raw ``Cookie:``
    header, or ``(None, None)``. Does NOT check the session is still live — see
    :func:`app.services.users.session_ok`."""
    if not cookie_header:
        return None, None
    try:
        jar = SimpleCookie()
        jar.load(cookie_header)
    except Exception:
        return None, None
    cookies = {k: m.value for k, m in jar.items()}
    try:
        from chainlit.auth.cookie import get_token_from_cookies
        from chainlit.auth.jwt import decode_jwt

        token = get_token_from_cookies(cookies)
        if not token:
            return None, None
        user = decode_jwt(token)
        email = getattr(user, "identifier", None) or None
        sid = (getattr(user, "metadata", None) or {}).get("sid")
        return email, (sid if isinstance(sid, str) else None)
    except Exception:
        return None, None


def live_email_from_cookie_header(cookie_header: str | None) -> tuple[str | None, str | None]:
    """``(email, sid)`` only if the session is still live and the user active."""
    email, sid = identity_from_cookie_header(cookie_header)
    if not email:
        return None, None
    from app.services import users

    return (email, sid) if users.session_ok(email, sid) else (None, None)


# ── Google OAuth ─────────────────────────────────────────────────────────

# state → (created_at, PKCE verifier). One process (uvicorn, 1 worker), so memory is enough.
_pending: dict[str, tuple[float, str]] = {}


def _gc_pending() -> None:
    cutoff = time.time() - STATE_TTL_S
    for k, (created, _) in list(_pending.items()):
        if created < cutoff:
            _pending.pop(k, None)


def build_authorize_url() -> tuple[str, str]:
    """``(url, state)`` for the Google consent redirect (PKCE S256)."""
    cid, _ = _client()
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    _gc_pending()
    _pending[state] = (time.time(), verifier)
    params = {
        "client_id": cid,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    return f"{AUTH_URL}?{urlencode(params)}", state


def consume_state(state: str, cookie_state: str | None) -> str | None:
    """Verify + atomically remove a pending state bound to this browser. Returns the PKCE
    verifier, or None if the state is unknown, expired, or from another browser."""
    _gc_pending()
    if not state or not cookie_state or not secrets.compare_digest(state, cookie_state):
        _pending.pop(state or "", None)
        return None
    item = _pending.pop(state, None)
    if item is None or time.time() - item[0] > STATE_TTL_S:
        return None
    return item[1]


async def exchange_code(code: str, verifier: str) -> dict:
    """Exchange an auth code for tokens; return the ID token's claims (unchecked)."""
    cid, csec = _client()
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": cid,
                "client_secret": csec,
                "redirect_uri": redirect_uri(),
                "grant_type": "authorization_code",
                "code_verifier": verifier,
            },
        )
    if r.status_code != 200:
        raise RuntimeError(f"token exchange failed {r.status_code}: {r.text[:200]}")
    return claims_of(r.json().get("id_token"))


def claims_of(id_token: str | None) -> dict:
    if not id_token or not isinstance(id_token, str):
        return {}
    try:
        parts = id_token.split(".")
        if len(parts) != 3:
            return {}
        payload = json.loads(_unb64(parts[1]).decode("utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def check_claims(claims: dict) -> str | None:
    """Why these ID-token claims are not acceptable, or None."""
    cid = os.getenv("VOITTA_GOOGLE_AUTH_CLIENT_ID")
    if not claims:
        return "no ID token"
    if claims.get("aud") != cid:
        return "issued for another app"
    if claims.get("iss") not in ISSUERS:
        return "not issued by Google"
    try:
        if float(claims.get("exp", 0)) < time.time():
            return "expired"
    except (TypeError, ValueError):
        return "expired"
    if not claims.get("sub") or not claims.get("email"):
        return "no account id or email"
    if claims.get("email_verified") not in (True, "true"):
        return "email not verified by Google"
    return None
