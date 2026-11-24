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

The Voitta Chrome extension (``extension/``) is the other relay, for pages whose CSP also forbids
``'unsafe-eval'``. Its service worker sends a tab's requests with ``Origin:
chrome-extension://<id>`` and the tab's browser-verified origin in the same tag. A trusted
extension origin counts as one of our own (its toolbar popup approves sites, as the bridge popup
does); any other extension origin is refused. Web pages can't send a ``chrome-extension://``
origin.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import time
from urllib.parse import parse_qs

from app.services.users import normalize_origin

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}

# The ID that extension/manifest.json's "key" pins, so an unpacked build has the same ID on
# every machine. VOITTA_TRUSTED_EXTENSIONS (comma- or space-separated IDs) replaces it, e.g.
# with a Web Store ID; set it empty to trust no extension.
DEFAULT_EXTENSION_IDS = ("cgcipboiieipdcbonhefjcgcikliboic",)
_EXT_PREFIX = "chrome-extension://"


def trusted_extension_origins() -> set[str]:
    raw = os.environ.get("VOITTA_TRUSTED_EXTENSIONS")
    ids = DEFAULT_EXTENSION_IDS if raw is None else re.split(r"[\s,]+", raw)
    out: set[str] = set()
    for x in ids:
        x = x.strip().lower().removeprefix(_EXT_PREFIX).rstrip("/")
        if re.fullmatch(r"[a-p]{32}", x):
            out.add(_EXT_PREFIX + x)
    return out


# Frame tickets. The extension's report frame (an extension page) loads a report as an iframe
# navigation, and Chrome sends neither Origin nor Referer for that, so the request carries no
# site at all. The frame first gets a ticket through an ordinary tagged request, which the
# guard only lets through for an approved site, then adds it to the report URL as
# ?voitta_ticket=. A ticket names the site and the path it is good for, expires, and is signed
# with a key that lives only in this process (a restart invalidates outstanding tickets).
TICKET_TTL_S = 600
_TICKET_KEY = secrets.token_bytes(32)


def _ticket_sig(exp: int, site: str, path: str) -> str:
    msg = f"{exp}|{site}|{path}".encode()
    return hmac.new(_TICKET_KEY, msg, hashlib.sha256).hexdigest()[:32]


def issue_ticket(site: str, path: str, *, now: float | None = None) -> str:
    exp = int((time.time() if now is None else now) + TICKET_TTL_S)
    head = base64.urlsafe_b64encode(f"{exp}|{site}".encode()).decode().rstrip("=")
    return f"{head}.{_ticket_sig(exp, site, path)}"


def ticket_site(token: str, path: str, *, now: float | None = None) -> str | None:
    """The site a valid, unexpired ticket for ``path`` names; else None."""
    try:
        head, sig = token.rsplit(".", 1)
        exp_s, site = base64.urlsafe_b64decode(head + "=" * (-len(head) % 4)).decode().split("|", 1)
        exp = int(exp_s)
    except (ValueError, UnicodeDecodeError):
        return None
    if exp < (time.time() if now is None else now):
        return None
    if not hmac.compare_digest(sig, _ticket_sig(exp, site, path)):
        return None
    return normalize_origin(site)


def _query(scope, key: str) -> str | None:
    vals = parse_qs((scope.get("query_string") or b"").decode("latin-1")).get(key)
    return vals[-1] if vals else None


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
    return _query(scope, "voitta_via")


def classify(scope) -> tuple[str, str | None]:
    """``("own"|"tool", None)``, ``("site", origin)`` or ``("refuse", reason)``."""
    h = _headers(scope)
    own = own_origins(scope)
    raw = h.get("origin")
    if raw is not None:
        if raw.strip() == "null":
            return "refuse", "opaque origin"
        ext = raw.strip().lower().rstrip("/")
        if ext.startswith(_EXT_PREFIX):
            if ext not in trusted_extension_origins():
                return "refuse", "untrusted extension"
            origin, same = None, True
        else:
            origin = normalize_origin(raw)
            if origin is None:
                return "refuse", "bad origin"
            same = origin in own
    else:
        ticket = _query(scope, "voitta_ticket")
        if ticket is not None:
            site = ticket_site(ticket, scope.get("path", ""))
            return ("site", site) if site else ("refuse", "bad ticket")
        sfs = h.get("sec-fetch-site")
        if sfs in ("cross-site", "same-site"):
            ref = normalize_origin(h.get("referer"))
            if ref is None or ref in own:
                return "refuse", "cross-site request without origin"
            return "site", ref
        # "none": typed URL / bookmark (the user, not a page), and also an extension's GET,
        # which Chrome sends without Origin; the relay tag below still applies to it.
        if sfs not in ("none", "same-origin"):
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
