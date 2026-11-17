"""hg.voitta.ai — the Hedgehog API front door (no sign-in).

Same server and process as bookmarklet.voitta.ai, but a separate world chosen by the request's Host:

* :mod:`app.hg.host`   — outermost ASGI layer: hg-host requests go to the hg app and NEVER reach the
                         main app (chat, settings, Python, admin, other plugins, /api, /chainlit…).
* :mod:`app.hg.store`  — keys + activity in their own ``hg.sqlite`` (never the user accounts).
* :mod:`app.hg.link`   — the panel in the user's StockTrak tab connects here with its key; API calls
                         for that key go to that key's most recently active tab, and only there.
* :mod:`app.hg.runner` — runs one Hedgehog tool for a key (shared by REST and MCP).
* :mod:`app.hg.api`    — the hg app: landing page, panel bundle, key routes, ``/v1`` REST, docs.
* :mod:`app.hg.mcp`    — ``/mcp`` with the Hedgehog tools only.

A key is the whole identity: no accounts, no cookies. It lives in the StockTrak site's own
storage in the user's browser, created by the panel on first use.
"""

from __future__ import annotations

import os

DEFAULT_HOSTS = "hg.voitta.ai"


def hosts() -> set[str]:
    raw = os.environ.get("VOITTA_HG_HOSTS", DEFAULT_HOSTS)
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def host_of(scope) -> str:
    for k, v in scope.get("headers", []):
        if k == b"host":
            h = v.decode("latin-1").strip().lower()
            if h.startswith("["):
                return h.split("]")[0] + "]"
            return h.rsplit(":", 1)[0] if h.count(":") == 1 else h
    return ""


def is_hg_request(scope) -> bool:
    return host_of(scope) in hosts()
