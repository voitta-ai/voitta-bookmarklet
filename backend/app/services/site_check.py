"""Which site is making this request? — the basis of site approval.

The widget runs inside other people's pages and talks to this backend with the user's cookie
(server) or with no credentials at all (desktop). The backend therefore can't tell the widget on
an approved site from a script on any page the user happens to open. The browser *can*: it stamps
every request with the page's origin (``Origin``; for no-cors GETs ``Sec-Fetch-Site`` +
``Referer``), and page scripts can't change that. :func:`classify` turns those headers into one of:

* ``own``    — our own pages (this backend's origin, or ``VOITTA_PUBLIC_BASE_URL``)
* ``site``   — another web page; it must be on the user's approved list
* ``tool``   — no browser metadata at all: a non-browser client (Claude Code via MCP, curl)
* ``refuse`` — browser request we can't attribute (opaque ``null`` origin, cross-site without
  origin/referer)

The hardened-site bridge (app.bridge) relays requests for the page that opened it; they reach us
same-origin, so the relay tags them with the opener's (browser-verified) origin in
``X-Voitta-Via`` (fetch) or ``?voitta_via=`` (WebSocket). That tag is only honoured on own-origin
requests, so a page can't use it to claim to be another site.
"""

from __future__ import annotations

import os
from urllib.parse import parse_qs

from app.services.users import normalize_origin

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def _headers(scope) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}


def host_of(scope) -> str:
    h = _headers(scope).get("host", "")
    if h.startswith("["):
        return h.split("]")[0] + "]"
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def own_origins(scope) -> set[str]:
    out: set[str] = set()
    base = normalize_origin(os.environ.get("VOITTA_PUBLIC_BASE_URL"))
    if base:
        out.add(base)
    host = _headers(scope).get("host")
    if host:
        scheme = "https" if scope.get("scheme") in ("https", "wss") else "http"
        mine = normalize_origin(f"{scheme}://{host}")
        if mine:
            out.add(mine)
    return out


def _via(scope, h: dict[str, str]) -> str | None:
    if h.get("x-voitta-via"):
        return h["x-voitta-via"]
    qs = parse_qs((scope.get("query_string") or b"").decode("latin-1"))
    vals = qs.get("voitta_via")
    return vals[-1] if vals else None


def classify(scope) -> tuple[str, str | None]:
    """``("own"|"tool", None)``, ``("site", origin)`` or ``("refuse", reason)``."""
    h = _headers(scope)
    own = own_origins(scope)
    raw = h.get("origin")
    if raw is not None:
        if raw.strip() == "null":
            return "refuse", "opaque origin"
        origin = normalize_origin(raw)
        if origin is None:
            return "refuse", "bad origin"
        same = origin in own
    else:
        sfs = h.get("sec-fetch-site")
        if sfs in ("cross-site", "same-site"):
            ref = normalize_origin(h.get("referer"))
            if ref is None or ref in own:
                return "refuse", "cross-site request without origin"
            return "site", ref
        if sfs == "none":  # typed URL / bookmark: the user, not a page
            return "own", None
        if sfs != "same-origin":
            return "tool", None
        origin, same = None, True
    if same:
        via = _via(scope, h)
        if via:
            vo = normalize_origin(via)
            if vo is None:
                return "refuse", "bad relay origin"
            return ("own", None) if vo in own else ("site", vo)
        return "own", None
    return "site", origin


def is_own(scope) -> bool:
    """Request comes from one of our own pages (never from a relayed or foreign site)."""
    return classify(scope)[0] == "own"
