"""LinkedIn tool registrations — thin shims over browser primitives
defined in plugins/linkedin/frontend/widget.ts.
"""

from __future__ import annotations

from typing import Any

from app.tools.browser import BrowserToolError, call_browser
from app.tools.registry import ToolCtx, ToolSpec, registry


# ---- linkedin_get_page_context -------------------------------------------


async def _get_page_context(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    try:
        info = await call_browser("linkedin_inspect_page", {}, ctx)
    except BrowserToolError as exc:
        return {"ok": False, "error": exc.kind, "message": str(exc)}
    return {"ok": True, **info}


registry.register(
    ToolSpec(
        name="linkedin_get_page_context",
        description=(
            "What page is the user looking at on linkedin.com? Returns "
            "{url, page_type, title, profile_id, company_slug, "
            "job_id, params}. "
            "page_type ∈ {feed, profile, company, jobs, job, "
            "messaging, mynetwork, notifications, search, other}.\n"
            "\n"
            "Call this first for any LinkedIn-flavoured task to learn "
            "which page the user is on."
        ),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_get_page_context,
        side="hybrid",
    )
)

# ---- linkedin_read_profile -----------------------------------------------

async def _read_profile(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if args.get("profile_id"):
        payload["profile_id"] = str(args["profile_id"])
    if args.get("sections"):
        payload["sections"] = [str(s) for s in args["sections"]]
    try:
        info = await call_browser("linkedin_read_profile", payload, ctx)
    except BrowserToolError as exc:
        return {"ok": False, "error": exc.kind, "message": str(exc)}
    return {"ok": True, **info}

registry.register(
    ToolSpec(
        name="linkedin_read_profile",
        description=(
            "Read a LinkedIn member profile as text, section by section — "
            "the tool for resumes, bios, candidate summaries, or any question "
            "about someone's background. Returns {profile_id, sections: "
            "{main, experience, education, skills, ...}, empty_or_missing, "
            "truncated}. `main` is the top card + About; each other key is "
            "the full /details/<section>/ list (not the 2-3 item preview).\n"
            "\n"
            "Defaults to the profile the user is looking at; pass profile_id "
            "(the <id> in /in/<id>/) for another one. Takes ~20-90 s — it "
            "loads each page in a hidden frame and waits for it to render.\n"
            "\n"
            "Use this instead of hand-rolled browser_eval scraping: LinkedIn "
            "renders profiles client-side, so fetching the HTML, parsing its "
            "JS bundles or calling its private APIs does not work.\n"
            "\n"
            "Errors: {ok: false, error: 'no_profile'} when the user isn't on "
            "a profile page and no profile_id was given."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "profile_id": {
                    "type": "string",
                    "description": "The <id> in linkedin.com/in/<id>/. Omit for the open profile.",
                },
                "sections": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Detail sections to read. Default: experience, education, "
                        "skills, certifications, projects, languages. Others: "
                        "honors, publications, courses, volunteering-experiences, "
                        "recommendations."
                    ),
                },
            },
            "additionalProperties": False,
        },
        handler=_read_profile,
        side="hybrid",
    )
)
