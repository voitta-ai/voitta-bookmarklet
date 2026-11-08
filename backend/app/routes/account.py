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
    return out


@router.delete("/sites")
async def remove_site(request: Request, origin: str) -> dict:
    users.remove_site(_owner(), origin, ip=client_ip(request))
    return {"ok": True, "sites": users.sites(_owner())}
