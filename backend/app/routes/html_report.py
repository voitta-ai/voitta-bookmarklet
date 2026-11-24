"""GET /api/html-report — serves a cached HTML-report body.
GET /api/html-report/export — the same body, packaged as an offline zip.

The body lives in :mod:`app.reports.renderers.html`'s in-process LRU
cache, keyed by ``(slug, render_id)``. The FE iframe pulls from this
route; the renderer populates the cache before the FE is told to
load it.

Same origin as the rest of the backend so the screenshot shim inside
the served HTML can talk to ``/api/report-render-events`` and friends
without CORS preflights.

The export shares the cache — and its eviction. A tab can outlive its
body (the cache holds 64 renders), in which case both routes answer
404 with the same "re-run the script" so the user gets one story.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from app.reports.export import build_zip, zip_filename
from app.reports.renderers.html import get_cached
from app.reports.slug import InvalidSlug, validate_slug
from app.services import site_check

router = APIRouter(prefix="/api")


def _lookup(id: str, render_id: str) -> str:
    try:
        validate_slug(id)
    except InvalidSlug as exc:
        raise HTTPException(400, str(exc)) from exc
    if not render_id or len(render_id) > 64:
        raise HTTPException(400, "bad render_id")
    body = get_cached(id, render_id)
    if body is None:
        raise HTTPException(
            404,
            f"no cached html-report for id={id!r} render_id={render_id!r} "
            f"— re-run the script",
        )
    return body


@router.post("/html-report/ticket")
async def html_report_ticket(request: Request) -> dict:
    """A frame ticket for ``/api/html-report`` (see ``site_check.issue_ticket``). The guard has
    already checked that the calling site is approved; own pages and tools need no ticket."""
    kind, origin = site_check.classify(request.scope)
    if kind != "site" or not origin:
        raise HTTPException(status_code=400, detail="Only a site needs a report ticket.")
    return {"ticket": site_check.issue_ticket(origin, "/api/html-report"), "ttl_s": site_check.TICKET_TTL_S}


@router.get("/html-report", response_class=HTMLResponse)
async def get_html_report(id: str, render_id: str) -> HTMLResponse:
    return HTMLResponse(_lookup(id, render_id))


@router.get("/html-report/export")
async def export_html_report(id: str, render_id: str, title: str | None = None) -> Response:
    body = _lookup(id, render_id)
    data = build_zip(body, slug=id, render_id=render_id, title=title)
    name = zip_filename(id, render_id)
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "no-store",
        },
    )
