"""Per-user plugin event log (see app.services.plugin_events).

* ``POST   /api/plugin-events/{plugin}``  {events: [...]}  → {kept}
* ``GET    /api/plugin-events/{plugin}?limit=``            → {events}
* ``DELETE /api/plugin-events/{plugin}``

Behind the main guard: the caller is our own page or an approved site and (server mode) signed
in, so a page can only write to its own user's log.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.services import plugin_events

router = APIRouter(prefix="/api/plugin-events")


@router.post("/{plugin}")
async def add_events(plugin: str, request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        body = None
    # sendBeacon posts text/plain; accept a bare list too.
    events = body.get("events") if isinstance(body, dict) else body
    try:
        return {"kept": plugin_events.append(plugin, events)}
    except plugin_events.EventError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/{plugin}")
async def get_events(plugin: str, limit: int = 300) -> dict:
    try:
        return {"events": plugin_events.read(plugin, limit)}
    except plugin_events.EventError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/{plugin}")
async def clear_events(plugin: str) -> dict:
    try:
        plugin_events.clear(plugin)
    except plugin_events.EventError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}
