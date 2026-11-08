"""Settings → Users (admins only, server mode): invite, roles, passwords, disable/enable, activity.

Behind the main guard (own page or approved site, live session); each route also requires the
caller to be an admin or owner. Owners (``VOITTA_OWNERS``) can't be demoted or disabled, nobody can
disable themselves, and every action is written to the activity log.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.routes.auth import client_ip
from app.services import login_auth, passwords, users
from app.services.current_user import get_current_email

router = APIRouter(prefix="/api/admin")


def _admin() -> str:
    if not login_auth.is_enabled():
        raise HTTPException(status_code=404, detail="No accounts on the desktop app.")
    email = get_current_email()
    if not users.is_admin(users.get(email)):
        raise HTTPException(status_code=403, detail="Admins only.")
    return email  # type: ignore[return-value]


async def _body(request: Request) -> dict:
    try:
        b = await request.json()
    except Exception:
        return {}
    return b if isinstance(b, dict) else {}


def _err(exc: users.UserError) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=400)


@router.get("/users")
async def list_users() -> dict:
    me = _admin()
    return {"me": me, "users": users.list_users()}


@router.get("/generate-password")
async def generate_password() -> dict:
    _admin()
    return {"password": passwords.generate()}


@router.post("/users/invite")
async def invite(request: Request):
    me = _admin()
    b = await _body(request)
    pw = b.get("password")
    pw = str(pw) if pw not in (None, "") else None
    try:
        results = users.invite(b.get("emails") or "", role=str(b.get("role") or "user"), password=pw,
                               actor=me, ip=client_ip(request))
    except users.UserError as exc:
        return _err(exc)
    return {"results": results, "users": users.list_users()}


@router.post("/users/{action}")
async def user_action(action: str, request: Request):
    me = _admin()
    b = await _body(request)
    email = str(b.get("email") or "")
    ip = client_ip(request)
    out: dict = {}
    try:
        if action == "role":
            users.set_role(email, str(b.get("role") or ""), actor=me, ip=ip)
        elif action == "password":
            mode = str(b.get("mode") or "set")
            keep = login_auth.identity_from_cookie_header(request.headers.get("cookie"))[1] \
                if users.normalize_email(email) == me else None
            if mode == "remove":
                users.set_password(email, None, actor=me, keep_sid=keep, ip=ip)
            else:
                pw = passwords.generate() if mode == "generate" else str(b.get("password") or "")
                users.set_password(email, pw, actor=me, keep_sid=keep, ip=ip)
                if mode == "generate":
                    out["password"] = pw
        elif action == "unlock":
            users.unlock(email, actor=me, ip=ip)
        elif action == "sign-out":
            users.revoke_all(email, actor=me, ip=ip)
        elif action == "disable":
            users.disable(email, actor=me, ip=ip)
        elif action == "enable":
            users.enable(email, actor=me, ip=ip)
        else:
            return JSONResponse({"detail": f"Unknown action {action}."}, status_code=404)
    except users.UserError as exc:
        return _err(exc)
    return {"ok": True, **out, "users": users.list_users()}


@router.get("/activity")
async def activity(limit: int = 200) -> dict:
    _admin()
    return {"activity": users.activity(limit)}
