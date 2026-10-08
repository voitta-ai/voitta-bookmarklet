"""``browser_eval`` — execute arbitrary JavaScript in the user's tab."""

from __future__ import annotations

import re
from typing import Any

from app.tools.browser import BrowserToolError, call_browser

# Script that reloads or navigates the tab. The bookmarklet lives in the
# page, so a full navigation ends the session mid-turn — the tool never
# answers and the user has to reopen the bookmarklet. SPA route changes
# (history.pushState) are fine and not matched.
_NAVIGATES = re.compile(
    r"\blocation\s*\.\s*(reload|assign|replace)\s*\("
    r"|\blocation\s*\.\s*href\s*=(?!=)"
    r"|\b(window|document|self|top)\s*\.\s*location\s*=(?!=)"
    r"|(?<![.\w])location\s*=(?!=)"
)

from app.tools.registry import ToolCtx, ToolSpec, registry


async def _handler(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    js = args.get("js")
    if not isinstance(js, str) or not js.strip():
        return {"ok": False, "error": "bad_request", "message": "js is required"}
    if _NAVIGATES.search(js) and not args.get("allow_navigation"):
        return {"ok": False, "error": "navigation_blocked",
                "message": ("This script reloads or navigates the page, which ends the "
                            "bookmarklet session (it lives in the page) — the result never "
                            "comes back. Use the site's own refresh/navigation primitive if "
                            "there is one. If a full navigation is really intended, ask the "
                            "user first, then pass allow_navigation=true.")}
    await_ms = int(args.get("await_ms") or 30_000)
    await_ms = max(100, min(120_000, await_ms))
    try:
        result = await call_browser(
            "eval_js", {"js": js, "await_ms": await_ms}, ctx, timeout_ms=await_ms + 5_000,
        )
    except BrowserToolError as exc:
        return {"ok": False, "error": exc.kind, "message": str(exc)}
    return result


registry.register(
    ToolSpec(
        name="browser_eval",
        description=(
            "Execute arbitrary JavaScript in the user's currently-bookmarklet'd "
            "browser tab.\n"
            "\n"
            "Runs in the page's origin, with full access to: document/DOM, "
            "localStorage, sessionStorage, document.cookie (non-HttpOnly), "
            "fetch (with the page's credentials), window globals, performance APIs.\n"
            "\n"
            "The body is wrapped in an async function. Top-level `await` works. "
            "Whatever you `return` from the script is sent back as the `result` "
            "field. Console output (log/warn/error) is captured into the `logs` "
            "array regardless of success.\n"
            "\n"
            "Inputs:\n"
            "  js       (string, required)  — JavaScript source. Must `return` "
            "the value you want the LLM to receive.\n"
            "  await_ms (integer, optional) — Hard timeout for the script. "
            "Default 30000, capped at 120000.\n"
            "\n"
            "Returns on success: {ok: true, result, logs: [{level, args}], ms}\n"
            "Returns on script throw: {ok: false, error: 'eval_threw', message, stack, logs, ms}\n"
            "Returns on transport failure: {ok: false, error: <kind>, message}\n"
            "\n"
            "Use this tool when no narrower plugin-provided primitive exists. "
            "When a purpose-built tool (e.g. linkedin_read_profile, ebay_scrape_search) "
            "is available for the task, prefer that — it's faster and the result "
            "shape is stable."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "js": {
                    "type": "string",
                    "description": "JavaScript source to execute. Must `return` the value you want back.",
                },
                "await_ms": {
                    "type": "integer",
                    "description": "Timeout in milliseconds. Default 30000, max 120000.",
                },
                "allow_navigation": {
                    "type": "boolean",
                    "description": ("Run a script that reloads or navigates the page. It ends "
                                    "the bookmarklet session; only after the user agreed."),
                },
            },
            "required": ["js"],
            "additionalProperties": False,
        },
        side="hybrid",
        global_tool=True,
        handler=_handler,
    )
)
