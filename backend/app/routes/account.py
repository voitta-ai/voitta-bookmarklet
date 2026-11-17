"""Settings → Account: who am I, and which sites may Voitta run on.

Behind the main guard, so the caller is our own page or an approved site, and (server mode) signed
in. Adding a site is NOT here: that only happens on the /auth/approve page (own origin).
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.routes.auth import client_ip
from app.services import login_auth, users
from app.services.current_user import get_current_email

router = APIRouter(prefix="/api/account")


def _owner() -> str:
    return (get_current_email() if login_auth.is_enabled() else None) or users.LOCAL_OWNER


@router.get("")
async def account() -> dict:
    out: dict = {"server": login_auth.is_enabled(), "sites": users.sites(_owner())}
    if login_auth.is_enabled():
        u = users.get(get_current_email())
        if u:
            out.update(email=u["email"], role=users.role_of(u), has_password=bool(u.get("password_hash")),
                       has_google=bool(u.get("google_sub")))
            out["api_keys"] = len(users.list_keys(u["email"]))
    return out


@router.delete("/sites")
async def remove_site(request: Request, origin: str) -> dict:
    users.remove_site(_owner(), origin, ip=client_ip(request))
    return {"ok": True, "sites": users.sites(_owner())}


# ── API keys for Claude Code (MCP), server mode ──────────────────────────


def _keys_user() -> str:
    from fastapi import HTTPException

    if not login_auth.is_enabled():
        raise HTTPException(status_code=404, detail="API keys are for the shared server; the desktop app's "
                                                    "/mcp is local and needs none.")
    email = get_current_email()
    if not email:
        raise HTTPException(status_code=401, detail="Sign in first.")
    return email


@router.get("/keys")
async def list_keys() -> dict:
    return {"keys": users.list_keys(_keys_user()), "scopes": list(users.SCOPES)}


@router.post("/keys")
async def create_key(request: Request):
    """Create a key. Our own pages only: an approved site may act as you while you're on it, but
    must not be able to mint a credential that keeps working from anywhere."""
    from fastapi.responses import JSONResponse

    from app.services import site_check

    email = _keys_user()
    if not site_check.is_own(request.scope):
        return JSONResponse({"detail": "Create keys on the Voitta page (/auth/keys)."}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}
    try:
        key, row = users.create_key(email, str(body.get("name") or ""), body.get("scopes"), ip=client_ip(request))
    except users.UserError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    return {"key": key, "info": row}


@router.delete("/keys/{key_id}")
async def revoke_key(key_id: int, request: Request) -> dict:
    from fastapi import HTTPException

    if not users.revoke_key(_keys_user(), key_id, ip=client_ip(request)):
        raise HTTPException(status_code=404, detail="No such key.")
    return {"ok": True, "keys": users.list_keys(_keys_user())}
