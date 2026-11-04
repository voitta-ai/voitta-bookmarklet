"""Routines routes — backing the chat-header alarm clock.

* ``GET  /api/routines``        — the user's routines
* ``PUT  /api/routines``        — replace the whole collection {routines: [...]}
* ``POST /api/routines/claim``  — {chat, routine_id, chime_id, slot_ms} →
  {ok: bool}; first tab to claim a slot fires it, the rest skip.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.services import routines

router = APIRouter(prefix="/api/routines")

async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    return body

@router.get("")
async def get_routines() -> dict:
    return {
        "routines": routines.list_routines(),
        "min_every_min": routines.MIN_EVERY_MIN,
    }

@router.put("")
async def put_routines(request: Request) -> dict:
    body = await _json_body(request)
    try:
        saved = routines.save_routines(body.get("routines"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"routines": saved, "min_every_min": routines.MIN_EVERY_MIN}

@router.post("/claim")
async def claim_slot(request: Request) -> dict:
    body = await _json_body(request)
    chat = str(body.get("chat") or "")
    routine_id = str(body.get("routine_id") or "")
    chime_id = str(body.get("chime_id") or "")
    try:
        slot_ms = int(body.get("slot_ms"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="slot_ms must be an integer")
    if not chat or not routine_id or not chime_id:
        raise HTTPException(status_code=400, detail="chat, routine_id and chime_id are required")
    return {"ok": routines.claim(chat, routine_id, chime_id, slot_ms)}
