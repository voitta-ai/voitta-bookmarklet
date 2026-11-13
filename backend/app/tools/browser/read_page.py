"""``read_page`` - read the page's DOM without evaluating JavaScript.

Hybrid wrapper around the FE's ``read_page`` primitive
([frontend/src/lib/primitives.ts]). It exists for pages whose
Content-Security-Policy forbids eval, where ``browser_eval`` cannot run.
"""

from __future__ import annotations

from typing import Any

from app.tools.browser import call_browser
from app.tools.registry import ToolCtx, ToolSpec, registry


async def _handler(args: dict[str, Any], ctx: ToolCtx) -> Any:
    retval = await call_browser("read_page", args, ctx)
    return retval


registry.register(
    ToolSpec(
        name="read_page",
        description=(
            "Read the page the user is viewing through the DOM, without running "
            "JavaScript: the rendered text or the HTML of the whole page, or of the "
            "elements that match a CSS selector. Read-only.\n"
            "\n"
            "It works on pages whose Content-Security-Policy forbids eval, where "
            "browser_eval fails with an EvalError.\n"
            "\n"
            "Inputs:\n"
            "  selector (string, optional) - CSS selector. Omitted: the page's <body>.\n"
            "  format   (string, optional) - 'text' (default) is the rendered text "
            "(innerText); 'html' is the outerHTML.\n"
            "\n"
            "Returns: {ok: true, url, title, format, selector, matches, truncated, items}. "
            "`matches` is how many elements matched. `items` holds their text or HTML in "
            "document order. Long content is cut off and `truncated` is true; pass a "
            "narrower selector to read the rest.\n"
            "\n"
            "It does not read inside iframes or shadow roots."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "selector": {
                    "type": "string",
                    "description": "CSS selector. Omitted: the page's <body>.",
                },
                "format": {
                    "type": "string",
                    "enum": ["text", "html"],
                    "description": "'text' (default): rendered text. 'html': outerHTML.",
                },
            },
            "additionalProperties": False,
        },
        side="hybrid",
        global_tool=True,
        handler=_handler,
    )
)
