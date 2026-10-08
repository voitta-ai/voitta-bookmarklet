"""FastAPI app: Chainlit at /chainlit, built FE at /, /health on the side."""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path

# Import app.config BEFORE any chainlit import: in server mode it sets the
# CHAINLIT_CUSTOM_AUTH / CHAINLIT_AUTH_SECRET / CHAINLIT_COOKIE_SAMESITE env
# vars that chainlit.auth reads at its own import time. (Loads .env too.)
import app.config as app_config  # side effects (auth env) MUST precede chainlit

from chainlit.utils import mount_chainlit
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.types import ASGIApp, Receive, Scope, Send


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# engineio hardcodes max_decode_packets=16, which is hit when the browser
# reconnects after a hang and batches up queued polling packets. Raise it.
try:
    import engineio.payload
    engineio.payload.Payload.max_decode_packets = 128
except Exception:
    pass


# Build the FastMCP sub-app BEFORE constructing FastAPI so we can hand
# its lifespan to the parent. FastMCP's streamable-HTTP transport
# spawns a task group inside its lifespan context — without it the
# StreamableHTTPSessionManager raises ``Task group is not initialized``
# on the first request. Per FastMCP's ASGI docs, the parent app must
# share the sub-app's lifespan.
try:
    from app.routes.mcp import build_mcp_asgi
    _mcp_asgi = build_mcp_asgi()
except Exception:
    logging.getLogger(__name__).exception("failed to build /mcp sub-app")
    _mcp_asgi = None


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Combined lifespan:

    * Enter the FastMCP sub-app's lifespan so its streamable-HTTP
      transport's task group is alive for the lifetime of the parent
      app (without this, /mcp 500s with "Task group is not initialized"
      on the first request).
    * Probe every plugin-declared MCP connector once at startup so the
      Settings panel lands warm. Best-effort — unreachable servers
      don't abort startup. Plugins themselves load when
      ``app.chainlit_app`` is imported via ``mount_chainlit`` below,
      so the connector list is populated by the time this fires.
    """
    async def _startup_tasks() -> None:
        # Hand the voice assistant a handle to this server's event loop
        # so its capture thread can inject utterances via call_fn. Must
        # run before the (possibly slow) MCP refresh below.
        try:
            import asyncio
            from app.services import voice
            voice.register_loop(asyncio.get_running_loop())
        except Exception:
            logging.getLogger(__name__).exception("voice loop registration failed")
        try:
            from app.services.mcp.registry import refresh_all
            await refresh_all()
        except Exception:
            logging.getLogger(__name__).exception("MCP startup refresh failed")
        # Warm the model catalog for every provider that already has a stored
        # credential (sync moment #3), so the Settings dropdown lands live.
        try:
            from app.services import models_catalog
            await models_catalog.warm_cached_credentialed()
        except Exception:
            logging.getLogger(__name__).exception("model-catalog warm-up failed")

    if _mcp_asgi is not None and hasattr(_mcp_asgi, "router"):
        async with _mcp_asgi.router.lifespan_context(_app):
            await _startup_tasks()
            yield
    else:
        await _startup_tasks()
        yield


app = FastAPI(title="voitta-compute", lifespan=_lifespan)


# ── Login guard (server mode only) ────────────────────────────────────────
# Active iff app.services.login_auth.is_enabled() — i.e. the Google login
# client creds are present in the env (.env), which only happens on a server,
# never in the macOS bundle or local dev. When active, requests to data
# endpoints must carry a valid signed session cookie; otherwise 401 (HTTP) or
# a handshake reject (WebSocket). Left OPEN so the login UI can bootstrap:
#   • /api/auth/*           — the login flow itself
#   • /health               — health probe
#   • frontend assets, /, /widget.js, /bridge/*  — so the widget can load and
#     render its "Sign in with Google" screen
# Guarded: /api/* (except /api/auth/*), /chainlit*, /mcp*.
#
# Added BEFORE CORSMiddleware so CORS stays OUTER of the guard and stamps its
# headers onto the 401 (the last add_middleware is the outermost layer).
from app.services import login_auth as _login_auth


def _is_guarded_path(path: str) -> bool:
    # /api/eval/ carries its own bearer-token auth (app.eval.api).
    if path == "/health" or path.startswith("/api/auth/") or path.startswith("/api/eval/"):
        return False
    return (
        path.startswith("/api/")
        or path.startswith("/chainlit")
        or path.startswith("/mcp")
    )


class _AuthGuard:
    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket") or not _login_auth.is_enabled():
            await self._app(scope, receive, send)
            return

        # Never block CORS/PNA preflights — they carry no cookies.
        if scope.get("method") == "OPTIONS" or not _is_guarded_path(scope.get("path", "")):
            await self._app(scope, receive, send)
            return

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        cookie_header = headers.get(b"cookie", b"").decode("latin-1")
        email = _login_auth.email_from_cookie_header(cookie_header)
        if email:
            # Carry the identity into the request so per-user data paths
            # (uploads serving, workspace routes, threads) resolve under this
            # user's folder. Same task as the downstream handler, so the
            # contextvar propagates; reset after to avoid cross-request bleed.
            from app.services.current_user import set_current_email, reset_current_email
            token = set_current_email(email)
            try:
                await self._app(scope, receive, send)
            finally:
                reset_current_email(token)
            return

        # Unauthenticated, guarded request.
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4401})
            return
        body = b'{"error":"login_required"}'
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})


app.add_middleware(_AuthGuard)

# CORS. The bookmarklet runs on third-party origins (drive.google.com,
# enterprise.voitta.ai, ...) and needs to send credentialed requests to
# the local backend so Chainlit's session cookie round-trips. The CORS
# spec forbids ``*`` together with credentials, so we echo the request
# Origin via a regex match — semantically equivalent for our threat
# model since auth lives in the cookie, not the origin.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=".*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

# Chrome Private Network Access gate — raw ASGI middleware so it wraps
# the entire stack (including CORSMiddleware and exception handlers) and
# cannot be bypassed by an error response.
#
# Chrome sends a combined CORS+PNA preflight (OPTIONS with
# Access-Control-Request-Private-Network: true) before any cross-origin
# request from a public origin to 127.0.0.1. Starlette's CORSMiddleware
# never echoes Access-Control-Allow-Private-Network, so Chrome blocks the
# actual request with net::ERR_FAILED.
#
# This middleware:
#  1. Short-circuits PNA preflights before they reach CORSMiddleware,
#     returning a 204 with all required headers in a single hop.
#  2. Injects Access-Control-Allow-Private-Network: true into every
#     non-preflight response at the send() level — immune to exceptions.
class _PNAMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        headers_raw: dict[bytes, bytes] = {
            k.lower(): v for k, v in scope.get("headers", [])
        }
        method: str = scope.get("method", "")

        # ── PNA preflight ────────────────────────────────────────────────
        if (
            method == "OPTIONS"
            and headers_raw.get(b"access-control-request-private-network") == b"true"
        ):
            origin = headers_raw.get(b"origin", b"*")
            response_headers = [
                (b"access-control-allow-origin",          origin),
                (b"access-control-allow-methods",         headers_raw.get(b"access-control-request-method",  b"GET, POST, DELETE, OPTIONS")),
                (b"access-control-allow-headers",         headers_raw.get(b"access-control-request-headers", b"*")),
                (b"access-control-allow-credentials",     b"true"),
                (b"access-control-allow-private-network", b"true"),
                (b"access-control-max-age",               b"86400"),
            ]
            await send({"type": "http.response.start", "status": 204, "headers": response_headers})
            await send({"type": "http.response.body",  "body": b""})
            return

        # ── All other requests: stamp PNA header on every response ───────
        async def _send_stamped(message: dict) -> None:
            if message["type"] == "http.response.start":
                hdrs = list(message.get("headers", []))
                hdrs.append((b"access-control-allow-private-network", b"true"))
                message = {**message, "headers": hdrs}
            await send(message)

        await self._app(scope, receive, _send_stamped)


app.add_middleware(_PNAMiddleware)


from app.bridge import router as bridge_router
from app.routes.auth import router as auth_router
from app.routes.google import router as google_router
from app.routes.html_report import router as html_report_router
from app.routes.models import router as models_router
from app.routes.plugins import router as plugins_router
from app.routes.reports import router as reports_router
from app.routes.settings import router as settings_router
from app.routes.workspace import router as workspace_router
from app.routes.agent_sdk import router as agent_sdk_router
from app.routes.projects import router as projects_router

app.include_router(auth_router)
app.include_router(projects_router)
app.include_router(reports_router)
app.include_router(html_report_router)
app.include_router(settings_router)
app.include_router(models_router)
app.include_router(plugins_router)
app.include_router(google_router)
app.include_router(workspace_router)
app.include_router(agent_sdk_router)

# Eval API (voitta-compute#19): mounted only when VOITTA_EVAL_TOKENS is set.
from app.eval import config as _eval_config  # noqa: E402

if _eval_config.enabled():
    from app.eval.api import router as eval_router  # noqa: E402
    from app.eval.trace import fail_abandoned as _eval_fail_abandoned  # noqa: E402

    for _d in _eval_config.all_tenant_dirs():
        _eval_fail_abandoned(_d / "runs")
    app.include_router(eval_router)
# Hardened-site bridge (/bridge, /bridge/relay.js, /bridge/boot.js). Must be
# registered before the catch-all frontend route so it isn't shadowed.
app.include_router(bridge_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# ── /chainlit/project/threads override (DESKTOP / no-auth only) ───────────
# Chainlit's built-in handler requires an authenticated user (401 otherwise).
# In desktop/dev there is no login, so we shadow it to serve the local user's
# thread history without auth.
#
# In SERVER MODE this override is NOT registered — Chainlit's native handler
# runs instead and filters threads by the authenticated user's id (and
# enforces is_thread_author on per-thread/resume reads). That is the native,
# per-user isolation we want; replicating it here would just risk drift.

async def project_threads(request: Request) -> dict:
    from chainlit.data import get_data_layer
    from chainlit.types import Pagination, ThreadFilter
    try:
        body = await request.json()
    except Exception:
        body = {}
    raw_pagination = body.get("pagination") or {}
    raw_filter = body.get("filter") or {}
    pagination = Pagination(
        first=raw_pagination.get("first", 20),
        cursor=raw_pagination.get("cursor"),
    )
    filters = ThreadFilter(
        feedback=raw_filter.get("feedback"),
        userId=raw_filter.get("userId"),
        search=raw_filter.get("search"),
    )
    data_layer = get_data_layer()
    if data_layer is None:
        return {"data": [], "pageInfo": {"hasNextPage": False, "startCursor": None, "endCursor": None}}
    result = await data_layer.list_threads(pagination, filters)
    if result is None:
        return {"data": [], "pageInfo": {"hasNextPage": False, "startCursor": None, "endCursor": None}}
    # PaginatedResponse is a dataclass — serialise to dict
    try:
        return result.dict() if hasattr(result, "dict") else {"data": result.data, "pageInfo": result.pageInfo}
    except Exception:
        return {"data": [], "pageInfo": {"hasNextPage": False, "startCursor": None, "endCursor": None}}


if not app_config.SERVER_MODE:
    app.add_api_route("/chainlit/project/threads", project_threads, methods=["POST"])


# Mount Chainlit at /chainlit. Its socket.io lives at
# /chainlit/ws/socket.io. MUST be mounted before the static FE catch-all
# below so it isn't shadowed.
_chainlit_target = Path(__file__).with_name("chainlit_app.py")
mount_chainlit(app=app, target=str(_chainlit_target), path="/chainlit")

# Patch sio connect handler AFTER mount_chainlit so the handler exists.
from app.chainlit_app import patch_sio_connect as _patch_sio_connect
_patch_sio_connect()


# Embedded FastMCP debugging server at /mcp. Gated by the
# ``mcpDebugEnabled`` user setting + loopback-only peer + Origin-absent
# checks — see app.routes.mcp for the middleware. Mounted AFTER
# chainlit so its routes don't get shadowed; mounted BEFORE the static
# FE catch-all below so `/mcp/*` paths reach this app. The sub-app
# itself is built at module load (above) so its lifespan can be merged
# into FastAPI's.
if _mcp_asgi is not None:
    app.mount("/mcp", _mcp_asgi)


# Reports are HTML-only. The /api/html-report route serves cached
# bodies and the /api/_panel_shim.js + /api/_html_to_image.js routes
# below provide the screenshot infrastructure injected into the
# LLM's <head> by app.reports.renderers.html.render_html.
#
# This shim is loaded into the HTML iframe via the <script src=...>
# injection above. It fires render-ready / render-error events back
# to ``/api/report-render-events`` so the awaiting agent-loop
# call_fn wakes up, and handles measure / reflow / screenshot
# postMessages from the parent's screenshot_report primitive.
_PANEL_SHIM_JS = """(function () {
  // Voitta report iframe shim — lean chainlit-build variant.
  //
  // Responsibilities:
  //   1. Signal "ready" once Bokeh's document has finished building.
  //   2. Capture render errors (window.error, unhandledrejection,
  //      Bokeh's "Error rendering Bokeh items" console pattern).
  //   3. Forward nested-iframe (three_scene) errors via the
  //      ``voitta_nested_error`` postMessage envelope.
  //   4. Forward render events to the parent via postMessage; the
  //      parent ReportPane forwards them to /api/report-render-events.
  //
  // The shim POSTs DIRECTLY to ``/api/report-render-events`` because
  // the report iframe is same-origin with the backend — no parent
  // round-trip needed. (Legacy build went through the parent so the
  // shim could handle the on-prem case; not relevant in chainlit-
  // local mode.)

  function readQS(name) {
    try {
      return new URLSearchParams(location.search).get(name) || "";
    } catch (_) { return ""; }
  }
  var SLUG = readQS("id");
  var RENDER_ID = readQS("render_id");

  var sentReady = false;
  var emittedErrors = 0;
  var ERR_CAP = 50;

  function postEvent(payload) {
    try {
      fetch("/api/report-render-events", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        // Same-origin — credentials not needed, but harmless.
        credentials: "same-origin",
      }).catch(function () { /* fire-and-forget */ });
    } catch (_) { /* swallow */ }
  }

  function emitReady() {
    if (sentReady) return;
    sentReady = true;
    postEvent({
      name: SLUG,
      kind: "ready",
      render_id: RENDER_ID,
      message: "report iframe ready",
      detail: {},
    });
  }

  function emitError(message, detail) {
    if (emittedErrors >= ERR_CAP) return;
    emittedErrors++;
    postEvent({
      name: SLUG,
      kind: "error",
      render_id: RENDER_ID,
      message: String(message || "").slice(0, 4000),
      detail: detail || {},
    });
  }

  // Window-level error capture.
  window.addEventListener("error", function (e) {
    var err = e && e.error;
    emitError((err && err.message) || e.message || String(e), {
      source: "window.error",
      stack: err && err.stack ? String(err.stack).slice(0, 6000) : null,
      url: e.filename || "",
      line: e.lineno || null,
      col: e.colno || null,
    });
  }, true);
  window.addEventListener("unhandledrejection", function (e) {
    var r = e && e.reason;
    emitError((r && (r.message || String(r))) || "unhandled rejection", {
      source: "unhandledrejection",
      stack: r && r.stack ? String(r.stack).slice(0, 6000) : null,
    });
  });
  // Bokeh emits "Error rendering Bokeh items" via console.error;
  // monkey-patch to forward.
  var _origCE = console.error.bind(console);
  console.error = function () {
    try {
      var parts = [];
      for (var i = 0; i < arguments.length; i++) {
        var a = arguments[i];
        if (a == null) { parts.push(""); continue; }
        if (typeof a === "string") { parts.push(a); continue; }
        if (a && a.message) {
          parts.push((a.name || "Error") + ": " + a.message);
          continue;
        }
        try { parts.push(String(a)); } catch (_) { parts.push("[unstringifiable]"); }
      }
      var joined = parts.join(" ");
      if (/Error rendering Bokeh items|Failed to (render|update)/i.test(joined)) {
        emitError(joined, { source: "console.error" });
      }
    } catch (_) {}
    return _origCE.apply(null, arguments);
  };

  // Listen for postMessages from nested srcdoc iframes (ctx.three_scene).
  // The three_scene viewer doc posts ``voitta_nested_error`` envelopes;
  // forward them as render errors.
  window.addEventListener("message", function (e) {
    var d = e && e.data;
    if (!d || typeof d !== "object") return;
    if (d.type === "voitta_nested_error") {
      emitError(d.message || "nested iframe error", {
        source: d.source || "nested",
        stack: d.stack,
        url: d.url,
        line: d.line,
        col: d.col,
      });
    }
  });

  // ─── multi-strategy content-extent measurement ───────────────────
  // Different reports need different measurement strategies. We
  // return all four candidates; the parent decides which to use.
  //
  //   body-scroll    — document.body.scrollHeight. Today's behavior.
  //                    Lies when body itself is stretch-fill (height:100%
  //                    or sizing_mode='stretch_*') — returns whatever
  //                    height the iframe was forced to, not content.
  //   deep-content   — Walk descendants of body, find max bbox.bottom
  //                    of non-stretch-fill elements. Drills through
  //                    stretch containers to find real content.
  //   bokeh-root     — Find Bokeh root containers (.bk-Column, .bk-GridBox,
  //                    [data-root-id]) and take their max bottom.
  //                    Cleanest for Panel/Bokeh reports specifically.
  //   generous       — A fixed large height (5000 px). Trust the
  //                    background-trim pass to crop the tail. Bulletproof.
  function _measureExtents() {
    var docEl = document.documentElement;
    var body = document.body;
    var scrollY = window.scrollY || 0;
    var result = {
      bodyScroll:  (body && body.scrollHeight) || docEl.scrollHeight,
      deepContent: 0,
      bokehRoot:   0,
      generous:    5000,
    };
    if (!body) return result;

    // deep-content: walk body's tree, find the max bottom edge of any
    // element whose CSS height is NOT 100% (stretch-fill). Stretch
    // containers' bottoms equal the iframe bottom, useless. We descend
    // INTO them but don't measure them directly.
    var maxBottom = 0;
    function isStretch(el) {
      try {
        var s = getComputedStyle(el);
        var h = s.height;
        var mh = s.minHeight;
        return h === "100%" || h === "100vh" || mh === "100%" || mh === "100vh";
      } catch (_) { return false; }
    }
    function walk(el, depth) {
      if (!el || depth > 32) return;
      if (el.nodeType !== 1) return;  // ELEMENT_NODE only
      var tag = el.tagName;
      if (tag === "SCRIPT" || tag === "STYLE" || tag === "META" || tag === "LINK") return;
      // Iframes' inner content can't be measured cross-origin, but
      // their bounding rect IS the right answer (the iframe itself
      // is a measurable box on the page).
      var rect;
      try { rect = el.getBoundingClientRect(); } catch (_) { return; }
      if (rect.width === 0 || rect.height === 0) {
        // Either invisible or empty; still walk children (a hidden
        // wrapper can contain visible descendants).
        for (var i = 0; i < el.children.length; i++) walk(el.children[i], depth + 1);
        return;
      }
      if (isStretch(el) && el.children.length) {
        // Stretch-fill container: ignore its bottom, descend.
        for (var j = 0; j < el.children.length; j++) walk(el.children[j], depth + 1);
        return;
      }
      var bottom = rect.bottom + scrollY;
      if (bottom > maxBottom) maxBottom = bottom;
      // Even non-stretch elements can have meaningful descendants
      // below their reported bbox if they have overflow:visible
      // children — descend a bit either way.
      for (var k = 0; k < el.children.length; k++) walk(el.children[k], depth + 1);
    }
    walk(body, 0);
    result.deepContent = Math.round(maxBottom);

    // bokeh-root: target Panel/Bokeh's known structural classes.
    var bokehMax = 0;
    var sels = ".bk-Column,.bk-GridBox,.bk-Row,.bk-panel-models-layout-Column," +
               ".bk-panel-models-layout-Row,[data-root-id]";
    try {
      var roots = body.querySelectorAll(sels);
      for (var r = 0; r < roots.length; r++) {
        try {
          var rr = roots[r].getBoundingClientRect();
          if (rr.height > 0) {
            var b = rr.bottom + scrollY;
            if (b > bokehMax) bokehMax = b;
          }
        } catch (_) {}
      }
    } catch (_) {}
    result.bokehRoot = Math.round(bokehMax);

    return result;
  }

  // ─── three.js / nested-iframe canvas capture ─────────────────────
  // ctx.three_scene mounts its WebGL canvas inside a srcdoc iframe.
  // Cross-origin sandboxing prevents the parent from reading those
  // pixels, AND html-to-image / dom-to-image-more / modern-screenshot
  // can't crawl into srcdoc-iframes either — they all see a blank
  // rectangle where the 3D scene should be. The three_scene viewer
  // sets `preserveDrawingBuffer: true` on its renderer and answers
  // `voitta_three_capture` postMessages with `canvas.toDataURL()`.
  // We ask each nested iframe for its snapshot, then blit them at
  // their on-page bounding-rect positions onto each technique's
  // output canvas before re-encoding.
  function _deepFindIframes(root, acc) {
    if (!root) return;
    try {
      var nodeIframes = root.querySelectorAll ? root.querySelectorAll("iframe") : [];
      for (var i = 0; i < nodeIframes.length; i++) acc.push(nodeIframes[i]);
      var all = root.querySelectorAll ? root.querySelectorAll("*") : [];
      for (var j = 0; j < all.length; j++) {
        if (all[j].shadowRoot) _deepFindIframes(all[j].shadowRoot, acc);
      }
    } catch (_) {}
  }
  function _captureNestedScenes() {
    var iframes = [];
    _deepFindIframes(document, iframes);
    if (!iframes.length) return Promise.resolve([]);
    var pending = iframes.map(function (iframe, idx) {
      var requestId = "three_cap_" + Date.now() + "_" + idx;
      return new Promise(function (resolve) {
        var done = false;
        function onMsg(ev) {
          if (!ev || !ev.data) return;
          if (ev.data.type !== "voitta_three_capture_response") return;
          if (ev.data.requestId !== requestId) return;
          if (done) return;
          done = true;
          window.removeEventListener("message", onMsg);
          if (ev.data.ok && ev.data.dataUrl) {
            var rect = iframe.getBoundingClientRect();
            resolve({
              dataUrl: ev.data.dataUrl,
              x: rect.x + (window.scrollX || 0),
              y: rect.y + (window.scrollY || 0),
              w: rect.width, h: rect.height,
            });
          } else {
            resolve(null);
          }
        }
        window.addEventListener("message", onMsg);
        try {
          iframe.contentWindow.postMessage(
            { type: "voitta_three_capture", requestId: requestId }, "*"
          );
        } catch (_) {}
        setTimeout(function () {
          if (done) return;
          done = true;
          window.removeEventListener("message", onMsg);
          resolve(null);
        }, 600);
      });
    });
    return Promise.all(pending).then(function (results) {
      return results.filter(function (r) { return r !== null; });
    });
  }

  // Load an Image from a data URL → resolved Image (ready to drawImage).
  function _loadImage(dataUrl) {
    return new Promise(function (resolve, reject) {
      var img = new Image();
      img.onload = function () { resolve(img); };
      img.onerror = function () { reject(new Error("image decode failed")); };
      img.src = dataUrl;
    });
  }

  // Take a base PNG data URL produced by a technique (which captured
  // a blank rect where each three.js iframe sits), composite each
  // nested scene snapshot at its on-page coordinates, return a new
  // PNG data URL. ``scale`` matches the technique's pixelRatio so
  // overlay coordinates line up with the technique's output bitmap.
  async function _compositeScenes(baseDataUrl, scenes, scale, fullW, fullH) {
    if (!scenes.length) return baseDataUrl;
    var base = await _loadImage(baseDataUrl);
    // Use the loaded image's natural pixel dimensions — different
    // techniques produce different output sizes (pixelRatio, etc.).
    var outW = base.naturalWidth, outH = base.naturalHeight;
    var canvas = document.createElement("canvas");
    canvas.width = outW; canvas.height = outH;
    var ctx2d = canvas.getContext("2d");
    ctx2d.drawImage(base, 0, 0);
    // Compute scale factors from page coordinates to output pixels.
    // The scene rects are in CSS pixels; the output bitmap is
    // CSS-pixel-width × scale. Use that ratio rather than the
    // caller-supplied scale, in case the technique applied its own.
    var sx = outW / fullW;
    var sy = outH / fullH;
    for (var i = 0; i < scenes.length; i++) {
      var snap = scenes[i];
      try {
        var img = await _loadImage(snap.dataUrl);
        ctx2d.drawImage(img,
          Math.round(snap.x * sx),
          Math.round(snap.y * sy),
          Math.round(snap.w * sx),
          Math.round(snap.h * sy));
      } catch (_) {
        // Individual scene-composite failure shouldn't break the
        // whole technique result — skip and continue.
      }
    }
    return canvas.toDataURL("image/png");
  }

  // Upload a data URL to the BE stash via same-origin HTTP. Returns
  // a stash id the parent can ferry through call_browser without
  // hitting socket.io's frame-size limit (multi-MB PNGs would silently
  // drop on the slow path). The stash endpoint is BE-served at the
  // iframe's own origin so this is a plain same-origin POST.
  async function _stashUpload(dataUrl, label, meta) {
    var comma = dataUrl.indexOf(",");
    var b64 = comma >= 0 ? dataUrl.slice(comma + 1) : dataUrl;
    var mime = dataUrl.indexOf("data:image/png") === 0 ? "image/png" : "image/webp";
    var res = await fetch("/api/screenshot-stash", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        label: label,
        media_type: mime,
        data: b64,
        meta: meta || {},
      }),
      credentials: "same-origin",
    });
    if (!res.ok) {
      throw new Error("stash POST failed: " + res.status + " " + res.statusText);
    }
    var json = await res.json();
    if (!json || !json.id) {
      throw new Error("stash POST returned no id");
    }
    return {
      id: json.id,
      media_type: mime,
      bytes_b64_len: b64.length,
    };
  }

  // Apply a hard timeout to a Promise; rejects with the given
  // message if it doesn't settle in ``ms``. The libraries we use
  // don't all bound their own internal CSS-rule probes — without
  // this, a failing remote-CSS fetch can hang a technique for many
  // seconds while the others wait. Each technique gets its own
  // budget so one slow lib doesn't kill the whole batch.
  function _withTimeout(promise, ms, label) {
    return new Promise(function (resolve, reject) {
      var t = setTimeout(function () {
        reject(new Error(label + " timed out after " + ms + "ms"));
      }, ms);
      promise.then(function (v) { clearTimeout(t); resolve(v); },
                   function (e) { clearTimeout(t); reject(e); });
    });
  }

  // Silence the known-noisy cross-origin CSS errors during capture.
  // Every technique tries to inline @import'd / <link> stylesheets;
  // when they're cross-origin (fonts.googleapis.com), CSSOM rule
  // access is blocked → each library logs and falls back to keeping
  // the <link> reference, which still works in the final SVG. We
  // restore the original console.error after capture so unrelated
  // errors keep surfacing.
  var _CSS_NOISE_RE = /cssRules|SecurityError|Error inlining remote css|Error while reading CSS rules|domtoimage: Error while reading CSS rules/;
  function _muteCSSNoise() {
    var orig = console.error;
    console.error = function () {
      try {
        var s = "";
        for (var i = 0; i < arguments.length; i++) {
          var a = arguments[i];
          if (a == null) continue;
          s += " " + (typeof a === "string" ? a : (a && a.message) || "");
        }
        if (_CSS_NOISE_RE.test(s)) return;  // suppress
      } catch (_) {}
      return orig.apply(this, arguments);
    };
    return function restore() { console.error = orig; };
  }

  // ─── multi-technique capture ─────────────────────────────────────
  // Runs every loaded screenshot library against the document body
  // and returns a list of {label, ok, dataUrl, ...} results. Each
  // technique is isolated — failures don't cascade. The parent
  // primitive ships ALL results back to the BE so the LLM can pick.
  //
  // Why three? html-to-image, dom-to-image-more, and modern-screenshot
  // each render CSS differently (drop shadows, backdrop-filter,
  // mix-blend-mode, custom fonts, mask-image). For a given report
  // one will look closest to the real thing. html2canvas was dropped
  // — empirically it truncates pyplot reports vs the SVG-foreignObject
  // techniques.
  //
  // Each technique's output is then composited with three.js scene
  // snapshots so WebGL canvases show up correctly.
  async function _captureMulti(opts) {
    var docEl = document.documentElement;
    var body = document.body;
    var fullW = Math.max(docEl.scrollWidth, docEl.clientWidth);
    var fullH = Math.max(docEl.scrollHeight, docEl.clientHeight);

    // Settle: same as the single-technique path. Wait for late
    // layout passes (Bokeh runs them after networkidle).
    await new Promise(function (r) {
      setTimeout(function () {
        requestAnimationFrame(function () {
          requestAnimationFrame(r);
        });
      }, opts.settleMs);
    });

    // Capture nested three.js scenes ONCE up front. Each technique
    // composites the same scene snapshots into its own bitmap.
    // Doing this once avoids hammering the nested iframes.
    var scenes;
    try { scenes = await _captureNestedScenes(); }
    catch (_) { scenes = []; }

    // Silence cross-origin CSS-rule warnings for the duration of
    // the techniques run. They're harmless but each library spams
    // them; in DevTools you'd see hundreds per screenshot call.
    var restoreConsole = _muteCSSNoise();

    // Read the document's actual background color so the rasteriser
    // fills transparent areas with the same colour the user sees.
    // getComputedStyle returns "rgba(0,0,0,0)" for transparent — in
    // that case fall back to white to avoid a black canvas.
    var _computedBg = getComputedStyle(body).backgroundColor;
    var _bgColor = (_computedBg && _computedBg !== "rgba(0, 0, 0, 0)" && _computedBg !== "transparent")
      ? _computedBg
      : (getComputedStyle(docEl).backgroundColor || "#ffffff");
    if (!_bgColor || _bgColor === "rgba(0, 0, 0, 0)" || _bgColor === "transparent") {
      _bgColor = "#ffffff";
    }

    // Single capture technique: html-to-image. Empirically the best
    // for Panel/Bokeh reports vs html2canvas (which truncated tall
    // pyplot reports) and the dom-to-image-more / modern-screenshot
    // alternates (which didn't render meaningfully differently).
    // SVG foreignObject capture, scenes composited separately.
    var techniques = [
      {
        label: "html-to-image",
        run: function () {
          if (!window.htmlToImage || typeof window.htmlToImage.toPng !== "function") {
            throw new Error("html-to-image not loaded");
          }
          return window.htmlToImage.toPng(body, {
            width: fullW, height: fullH,
            pixelRatio: opts.scale,
            backgroundColor: _bgColor,
            cacheBust: true,
          });
        },
      },
    ];

    // Per-technique hard timeout. 12s is well above the typical
    // ~500ms each library actually needs; hitting it means the
    // library is genuinely stuck (usually on a cross-origin fetch
    // its CSS-inlining path can't bound). Truncates so the other
    // techniques + the strategy loop still ship a result.
    var TECHNIQUE_TIMEOUT_MS = 12000;

    var results = [];
    try {
      for (var i = 0; i < techniques.length; i++) {
        var t = techniques[i];
        var t0 = (performance && performance.now) ? performance.now() : Date.now();
        try {
          var dataUrl = await _withTimeout(
            Promise.resolve().then(t.run),
            TECHNIQUE_TIMEOUT_MS,
            t.label,
          );
          try {
            dataUrl = await _compositeScenes(dataUrl, scenes, opts.scale, fullW, fullH);
          } catch (compErr) {
            // Composite failure → keep the raw technique output rather
            // than failing the whole result. The 3D scene will look
            // blank but the rest of the report is still useful.
          }
          // Stash the bytes BE-side over a plain same-origin POST,
          // then return only the stash id through postMessage. Going
          // through the parent + the chainlit socket with multi-MB
          // PNGs silently truncates (frame-size limit), so we
          // bypass that path entirely for the pixel payload.
          var stashInfo = await _stashUpload(dataUrl, t.label, {
            technique: t.label,
            width: fullW, height: fullH,
            scenes_composited: scenes.length,
          });
          var t1 = (performance && performance.now) ? performance.now() : Date.now();
          results.push({
            label: t.label, ok: true,
            stash_id: stashInfo.id,
            media_type: stashInfo.media_type,
            bytes_b64_len: stashInfo.bytes_b64_len,
            width: fullW, height: fullH,
            ms: Math.round(t1 - t0),
            scenes_composited: scenes.length,
          });
        } catch (err) {
          var t1e = (performance && performance.now) ? performance.now() : Date.now();
          results.push({
            label: t.label, ok: false,
            message: String((err && err.message) || err),
            ms: Math.round(t1e - t0),
          });
        }
      }
    } finally {
      // ALWAYS restore the original console.error, even if a
      // technique threw out from under the timeout. Otherwise the
      // mute leaks across screenshot calls and unrelated errors
      // get silently dropped.
      try { restoreConsole(); } catch (_) {}
    }
    return results;
  }

  // ─── parent → iframe action bridge ───────────────────────────────
  // The parent's screenshot_report primitive postMessages a
  // ``voittaAction: measure`` (to learn natural height) then a
  // ``voittaAction: screenshot`` (to rasterise via html2canvas).
  // Replies go back as ``type: voitta_report_event``.
  window.addEventListener("message", function (e) {
    if (e.source !== window.parent) return;
    var a = e.data && e.data.voittaAction;
    if (a === "reflow") {
      // Force Bokeh/Panel to re-evaluate layout after the parent
      // resized our iframe. Bokeh apps cache layout from the initial
      // viewport and won't reflow on iframe resize unless a real
      // ``resize`` event fires. Dispatching one here + awaiting two
      // RAFs lets Bokeh's resize handler run and the new DOM settle
      // before screenshot.
      var frid = e.data.requestId;
      var fSettleMs = +e.data.settle_ms || 600;
      try { window.dispatchEvent(new Event("resize")); } catch (_) {}
      setTimeout(function () {
        requestAnimationFrame(function () {
          requestAnimationFrame(function () {
            try {
              window.parent.postMessage({
                type: "voitta_report_event",
                kind: "reflow_response",
                requestId: frid,
                innerW: window.innerWidth,
                innerH: window.innerHeight,
              }, "*");
            } catch (_) { /* parent gone */ }
          });
        });
      }, fSettleMs);
      return;
    }
    if (a === "measure") {
      var mrid = e.data.requestId;
      var mSettleMs = +e.data.settle_ms || 400;
      setTimeout(function () {
        requestAnimationFrame(function () {
          requestAnimationFrame(function () {
            var docElM = document.documentElement;
            try {
              var extents = _measureExtents();
              window.parent.postMessage({
                type: "voitta_report_event",
                kind: "measure_response",
                requestId: mrid,
                // Legacy fields — keep so existing callers still work.
                scrollH: docElM.scrollHeight,
                clientH: docElM.clientHeight,
                bodyScrollH: (document.body && document.body.scrollHeight) || 0,
                innerH: window.innerHeight,
                // New: per-strategy content-extent candidates.
                extents: extents,
              }, "*");
            } catch (_) { /* parent gone */ }
          });
        });
      }, mSettleMs);
      return;
    }
    if (a === "screenshot_multi") {
      // Multi-technique capture. Runs four different screenshot
      // libraries against the same DOM and returns all results as
      // a single response so the LLM can pick the best-looking
      // candidate. Each technique is independent — one failing
      // doesn't affect the others.
      //
      // Techniques attempted (in order):
      //   1. html2canvas       — canvas re-paint (with three_scene compositing)
      //   2. html-to-image     — SVG foreignObject (toPng)
      //   3. dom-to-image-more — SVG foreignObject, different impl
      //   4. modern-screenshot — latest SVG-foreignObject implementation
      //
      // Note: techniques 2-4 don't get three_scene compositing
      // automatically — they use SVG foreignObject which captures
      // computed CSS rather than re-painting. Embedded canvases
      // (the 3D scene) WILL render in those technique outputs as
      // their CSS snapshot, but won't include the WebGL contents.
      // Only html2canvas + our compositor handles 3D content.
      var mrid = e.data.requestId;
      var mScale = +e.data.scale || 1;
      var mFormat = e.data.format === "png" ? "image/png" : "image/webp";
      var mQuality = +e.data.quality || 0.85;
      var mSettleMs = (e.data && +e.data.settle_ms) || 600;
      function mreply(payload) {
        try {
          window.parent.postMessage(Object.assign(
            { type: "voitta_report_event", kind: "screenshot_multi_response", requestId: mrid },
            payload
          ), "*");
        } catch (_) {}
      }
      _captureMulti({
        scale: mScale, format: mFormat, quality: mQuality, settleMs: mSettleMs,
      }).then(function (results) {
        mreply({ ok: true, results: results });
      }).catch(function (err) {
        mreply({ ok: false, message: String((err && err.message) || err) });
      });
      return;
    }
    if (a === "screenshot") {
      var rid = e.data.requestId;
      var scale = +e.data.scale || 1;
      var quality = +e.data.quality || 0.85;
      var format = e.data.format === "png" ? "image/png" : "image/webp";
      var settleMs = (e.data && +e.data.settle_ms) || 600;
      function sreply(payload) {
        try {
          window.parent.postMessage(Object.assign(
            { type: "voitta_report_event", kind: "screenshot_response", requestId: rid },
            payload
          ), "*");
        } catch (_) { /* parent gone */ }
      }
      if (typeof html2canvas !== "function") {
        sreply({ ok: false, message: "html2canvas not loaded" });
        return;
      }
      function _afterSettle(cb) {
        setTimeout(function () {
          requestAnimationFrame(function () {
            requestAnimationFrame(cb);
          });
        }, settleMs);
      }
      _afterSettle(function () {
        var docEl = document.documentElement;
        var fullW = Math.max(docEl.scrollWidth, docEl.clientWidth);
        var fullH = Math.max(docEl.scrollHeight, docEl.clientHeight);
        var bodyScrollH = (document.body && document.body.scrollHeight) || 0;
        var dims = {
          scrollW: docEl.scrollWidth, scrollH: docEl.scrollHeight,
          clientW: docEl.clientWidth, clientH: docEl.clientHeight,
          innerW: window.innerWidth, innerH: window.innerHeight,
          bodyScrollH: bodyScrollH,
          chosenW: fullW, chosenH: fullH,
        };

        // Three_scene nested iframes — their canvases are cross-origin
        // to us, so html2canvas alone sees a blank rectangle. Ask each
        // iframe for a canvas.toDataURL() via the voitta_three_capture
        // protocol, then composite after rasterisation.
        function _deepFindIframes(root, acc) {
          if (!root) return;
          try {
            var nodeIframes = root.querySelectorAll ? root.querySelectorAll("iframe") : [];
            for (var i = 0; i < nodeIframes.length; i++) acc.push(nodeIframes[i]);
            var all = root.querySelectorAll ? root.querySelectorAll("*") : [];
            for (var j = 0; j < all.length; j++) {
              if (all[j].shadowRoot) _deepFindIframes(all[j].shadowRoot, acc);
            }
          } catch (_) {}
        }
        function captureNestedScenes() {
          var iframes = [];
          _deepFindIframes(document, iframes);
          if (!iframes.length) return Promise.resolve([]);
          var pending = iframes.map(function (iframe, idx) {
            var requestId = "three_cap_" + Date.now() + "_" + idx;
            return new Promise(function (resolve) {
              var done = false;
              function onMsg(ev) {
                if (!ev || !ev.data) return;
                if (ev.data.type !== "voitta_three_capture_response") return;
                if (ev.data.requestId !== requestId) return;
                if (done) return;
                done = true;
                window.removeEventListener("message", onMsg);
                if (ev.data.ok && ev.data.dataUrl) {
                  var rect = iframe.getBoundingClientRect();
                  resolve({
                    dataUrl: ev.data.dataUrl,
                    x: rect.x + (window.scrollX || 0),
                    y: rect.y + (window.scrollY || 0),
                    w: rect.width, h: rect.height,
                  });
                } else {
                  resolve(null);
                }
              }
              window.addEventListener("message", onMsg);
              try {
                iframe.contentWindow.postMessage(
                  { type: "voitta_three_capture", requestId: requestId }, "*"
                );
              } catch (_) {}
              setTimeout(function () {
                if (done) return;
                done = true;
                window.removeEventListener("message", onMsg);
                resolve(null);
              }, 600);
            });
          });
          return Promise.all(pending).then(function (results) {
            return results.filter(function (r) { return r !== null; });
          });
        }

        try {
          Promise.all([
            html2canvas(docEl, {
              allowTaint: true, useCORS: true, foreignObjectRendering: false,
              scale: scale, width: fullW, height: fullH,
              windowWidth: fullW, windowHeight: fullH,
              backgroundColor: _bgColor,
              logging: false,
            }),
            captureNestedScenes(),
          ]).then(function (arr) {
            var canvas = arr[0];
            var sceneSnaps = arr[1];

            // Trim trailing background-colored rows at the bottom of
            // the canvas. Stretch-fill reports get force-expanded to a
            // generous canvas (the parent over-allocates so content has
            // room to lay out), so the tail ends up filled with the
            // page's background color. We DON'T assume white — dark
            // themes paint the bottom black, light themes white.
            //
            // Algorithm:
            //   1. Sample the bottom-right corner — that's almost
            //      certainly background (the report's title and KPIs
            //      sit at the top-left).
            //   2. Scan rows from bottom up, sampling every 8th column.
            //      A row "has content" if ANY sampled pixel differs
            //      from the corner reference by > BG_DELTA in any
            //      channel. 18 is robust to anti-aliasing on solid
            //      backgrounds without false-firing on subtle gradients.
            //   3. The last content row defines the crop; we keep a
            //      40-px margin below it so the bottom edge breathes.
            //
            // Tainted-canvas / OOM during getImageData → return as-is.
            function trimBottomWhitespace(c) {
              var tw = c.width, th = c.height;
              if (th < 200) return c;
              var ctxT = c.getContext("2d");
              var data;
              try { data = ctxT.getImageData(0, 0, tw, th).data; }
              catch (e) {
                console.warn("[screenshot] trim: getImageData failed (tainted canvas?)", e);
                return c;
              }
              // Sample the background from MULTIPLE corners — if the
              // bottom-right corner happens to be content (e.g. a
              // top-aligned diagram is so narrow it doesn't reach the
              // right edge, but the trim probe sees something else),
              // we use the most common corner color. All 4 corners
              // agreeing strongly implies "this is the background".
              function rgb(idx) { return [data[idx], data[idx+1], data[idx+2]]; }
              var corners = [
                rgb(0),                                    // top-left
                rgb((tw - 1) * 4),                         // top-right
                rgb(((th - 1) * tw) * 4),                  // bottom-left
                rgb(((th - 1) * tw + (tw - 1)) * 4),       // bottom-right
              ];
              // Take median of corners — robust to one corner being
              // non-background.
              corners.sort(function(a, b) {
                return (a[0] + a[1] + a[2]) - (b[0] + b[1] + b[2]);
              });
              var ref = corners[1]; // second-smallest = stable middle
              var refR = ref[0], refG = ref[1], refB = ref[2];

              var BG_DELTA = 18;
              function differs(idx) {
                return (
                  Math.abs(data[idx]     - refR) > BG_DELTA ||
                  Math.abs(data[idx + 1] - refG) > BG_DELTA ||
                  Math.abs(data[idx + 2] - refB) > BG_DELTA
                );
              }
              var step = 8;
              var lastContent = -1;
              for (var y = th - 1; y >= 0; y--) {
                var hit = false;
                for (var x = 0; x < tw; x += step) {
                  if (differs((y * tw + x) * 4)) { hit = true; break; }
                }
                if (hit) { lastContent = y; break; }
              }
              var newH = (lastContent >= 0 ? lastContent + 40 : th);
              if (newH >= th) {
                console.info("[screenshot] trim: no background tail found",
                  { th: th, ref: ref, lastContent: lastContent });
                return c;
              }
              console.info("[screenshot] trim:", th, "->", newH,
                "(saved", th - newH, "px)", { ref: ref });
              var trimmed = document.createElement("canvas");
              trimmed.width = tw;
              trimmed.height = newH;
              trimmed.getContext("2d").drawImage(c, 0, 0);
              return trimmed;
            }

            function emit() {
              var finalCanvas = canvas;
              try { finalCanvas = trimBottomWhitespace(canvas); }
              catch (_) { finalCanvas = canvas; }
              var dataUrl;
              try { dataUrl = finalCanvas.toDataURL(format, quality); }
              catch (err) {
                sreply({ ok: false, message: "canvas_tainted: " + (err && err.message) });
                return;
              }
              sreply({
                ok: true, dataUrl: dataUrl,
                width: finalCanvas.width, height: finalCanvas.height,
                full_width: fullW, full_height: fullH,
                scale: scale, format: format,
                nested_scenes_captured: sceneSnaps.length,
                doc_dims: dims,
                trimmed_from_h: canvas.height,
              });
            }

            if (!sceneSnaps.length) { emit(); return; }
            // Composite-then-emit: chain image loads.
            var pending = sceneSnaps.map(function (snap) {
              return new Promise(function (res) {
                var img = new Image();
                img.onload = function () {
                  try {
                    canvas.getContext("2d").drawImage(img,
                      snap.x * scale, snap.y * scale,
                      snap.w * scale, snap.h * scale);
                  } catch (_) {}
                  res();
                };
                img.onerror = function () { res(); };
                img.src = snap.dataUrl;
              });
            });
            Promise.all(pending).then(emit);
          }).catch(function (err) {
            sreply({ ok: false, message: String((err && err.message) || err) });
          });
        } catch (err) {
          sreply({ ok: false, message: String((err && err.message) || err) });
        }
      });
      return;
    }
  });

  // Fire "ready" once Bokeh's document is fully built. Bokeh emits a
  // ``document_ready`` event on ``Bokeh.documents`` per-doc; we poll
  // a few times because the global is defined progressively.
  function pollReady() {
    var docs = (window.Bokeh && window.Bokeh.documents) || [];
    if (docs.length > 0 && docs.every(function (d) { return d.is_idle; })) {
      emitReady();
      return;
    }
    setTimeout(pollReady, 200);
  }
  // Also fire on plain ``load`` so a Panel page with no Bokeh doc
  // (pure pn.pane.HTML / Markdown) still signals ready.
  window.addEventListener("load", function () {
    setTimeout(function () {
      var docs = (window.Bokeh && window.Bokeh.documents) || [];
      if (docs.length === 0) emitReady();
    }, 100);
    pollReady();
  });
})();
"""


@app.get("/api/_panel_shim.js")
async def panel_shim_js() -> "Response":
    from fastapi.responses import Response

    return Response(content=_PANEL_SHIM_JS, media_type="application/javascript")


def _shim_js_for_html_report() -> str:
    """Expose the shim JS body so the HTML-report renderer can inline it
    into its base template. The HTML-report iframe is same-origin (it
    loads from our backend), but inlining avoids an extra GET — and it
    means the same string is the source of truth for both paths."""
    return _PANEL_SHIM_JS


def _serve_node_module(rel_path: str, package_label: str) -> FileResponse:
    """Serve a screenshot JS library.

    Resolution order:
      1. resources/vendor_js/<filename>  — present in the frozen .app
         (staged by build_app.sh from frontend/node_modules/).
      2. frontend/node_modules/<rel_path> — dev checkout fallback.
    """
    from fastapi import HTTPException

    filename = rel_path.split("/")[-1]

    # 1. Bundle resources (frozen .app)
    try:
        import voitta_compute
        bundle = Path(voitta_compute.__file__).resolve().parent / "resources" / "vendor_js" / filename
        if bundle.is_file():
            return FileResponse(
                bundle, media_type="application/javascript",
                headers={"Cache-Control": "public, max-age=86400"},
            )
    except ImportError:
        pass

    # 2. Dev: frontend/node_modules/
    path = Path(__file__).resolve().parents[2] / "frontend" / "node_modules"
    for part in rel_path.split("/"):
        path = path / part
    if not path.exists():
        raise HTTPException(
            status_code=503,
            detail=(
                f"{package_label} not installed; run `npm install` in "
                f"frontend/. Expected {path}"
            ),
        )
    return FileResponse(
        path, media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/api/_html2canvas.js")
async def html2canvas_js():
    """Serve the html2canvas library into the report iframe.

    Loaded by the report iframe's shim, which calls
    ``window.html2canvas(...)`` for the canonical capture path. ~200 KB.
    """
    return _serve_node_module(
        "html2canvas/dist/html2canvas.min.js", "html2canvas",
    )


@app.get("/api/_html_to_image.js")
async def html_to_image_js():
    """Serve the html-to-image library into the report iframe.

    Alternate capture technique that uses SVG foreignObject snapshots
    instead of canvas re-paint. Often handles CSS features
    (mix-blend, complex shadows) better than html2canvas; sometimes
    worse for foreign-fonts. Exposes ``window.htmlToImage``. ~36 KB.
    """
    return _serve_node_module(
        "html-to-image/dist/html-to-image.js", "html-to-image",
    )




# ─────────────────────────────────────────────────────────────────────
# Screenshot stash — in-memory cache for screenshot bytes too large to
# round-trip through the Chainlit socket.
#
# When ``screenshot_report`` runs in multi-strategy/multi-technique
# mode it produces 9+ PNGs (each 1-5 MB). Sending them all through
# ``CopilotFunction.acall()`` exceeds socket.io's default frame size
# and the response silently drops. So the FE POSTs each PNG here
# directly (same-origin HTTP, no frame limit), and only sends the
# resulting stash IDs through the slow path. The agent loop fetches
# the bytes back via :func:`_screenshot_stash_get`, attaches them to
# the Chainlit step as elements, then evicts.
#
# Memory bound: ``_STASH_MAX`` entries, FIFO eviction. Each entry
# survives ``_STASH_TTL_S`` seconds — long enough for the agent loop
# to consume but short enough that a stuck call doesn't leak.
# ─────────────────────────────────────────────────────────────────────

import time
import uuid
from collections import OrderedDict
from threading import Lock

_STASH_MAX = 64
_STASH_TTL_S = 300

_screenshot_stash: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
_screenshot_stash_lock = Lock()


def _screenshot_stash_evict_expired() -> None:
    """Drop entries past TTL. Called opportunistically on each put/get."""
    now = time.time()
    with _screenshot_stash_lock:
        stale = [k for k, v in _screenshot_stash.items()
                 if now - v["created_at"] > _STASH_TTL_S]
        for k in stale:
            _screenshot_stash.pop(k, None)


def _screenshot_stash_put(media_type: str, data_b64: str, meta: dict[str, Any]) -> str:
    """Stash a base64 PNG/WebP. Returns a stash id the FE pipes through."""
    _screenshot_stash_evict_expired()
    sid = uuid.uuid4().hex
    with _screenshot_stash_lock:
        # FIFO cap
        while len(_screenshot_stash) >= _STASH_MAX:
            _screenshot_stash.popitem(last=False)
        _screenshot_stash[sid] = {
            "media_type": media_type,
            "data": data_b64,
            "meta": meta,
            "created_at": time.time(),
        }
    return sid


def _screenshot_stash_pop(sid: str) -> dict[str, Any] | None:
    """Consume and remove a stashed image. Returns None if missing."""
    with _screenshot_stash_lock:
        return _screenshot_stash.pop(sid, None)


@app.post("/api/screenshot-stash")
async def screenshot_stash_put(request: Request) -> dict[str, Any]:
    """Accept one screenshot PNG/WebP from the FE.

    Body: ``{label, media_type, data, meta?}`` where ``data`` is a
    base64-encoded image (NOT the full data URL — just the bytes).

    Returns: ``{id}``. The FE includes these IDs in its tool result
    via the ``_images_stash`` sentinel; the agent loop unpacks them
    by calling :func:`_screenshot_stash_pop` for each.

    Same-origin only (the bookmarklet's iframe is BE-served).
    """
    from fastapi import HTTPException
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    media_type = body.get("media_type")
    data = body.get("data")
    if not isinstance(media_type, str) or not isinstance(data, str):
        raise HTTPException(
            status_code=400,
            detail="missing required string fields: media_type, data",
        )
    meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
    label = body.get("label")
    if isinstance(label, str):
        meta = {**meta, "label": label}
    sid = _screenshot_stash_put(media_type, data, meta)
    return {"ok": True, "id": sid}


from app.config import FRONTEND_DIST as _FE_DIST, USER_DATA_ROOT as _USER_DATA_ROOT

# Serve locally-stored element uploads (images, files attached to messages).
# Per-user in server mode: the directory resolves under the current user's
# folder (user_data_root()/uploads), so a user can only fetch their own
# attachments — the request's auth cookie sets the contextvar in _AuthGuard
# before this handler runs. Desktop/dev resolves to USER_DATA_ROOT/uploads,
# unchanged. A dynamic route (not a static mount) is required because the
# directory is per-request, not fixed at startup.
from fastapi import HTTPException as _HTTPException


@app.get("/api/uploads/{key:path}", include_in_schema=False)
async def serve_upload(key: str) -> FileResponse:
    from app.services.projects import project_data_root
    base = (project_data_root() / "uploads").resolve()
    target = (base / key).resolve()
    # Path-traversal guard: target must stay inside this user's uploads dir.
    if not str(target).startswith(str(base) + "/") and target != base:
        raise _HTTPException(status_code=404)
    if not target.is_file():
        raise _HTTPException(status_code=404)
    return FileResponse(str(target))


_NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate"}


@app.get("/", include_in_schema=False)
async def root(request: Request):
    """Server mode: the root shows the bookmarklets page (install instructions
    are the natural landing here — the widget is used via bookmarklet, not by
    visiting the backend). Desktop/dev: unchanged — serve the frontend so
    visiting https://127.0.0.1:12358/ still shows the widget."""
    if app_config.SERVER_MODE:
        from app.bridge import render_bookmarklets
        from fastapi.responses import HTMLResponse as _HTMLResponse
        return _HTMLResponse(render_bookmarklets(request), headers=_NO_CACHE)
    return await serve_frontend("")


@app.get("/{full_path:path}", include_in_schema=False)
async def serve_frontend(full_path: str) -> FileResponse:
    if not _FE_DIST.is_dir():
        return FileResponse(str(Path(__file__).with_name("missing_frontend.html")))
    target = _FE_DIST / full_path
    if full_path and target.is_file():
        return FileResponse(str(target), headers=_NO_CACHE)
    index = _FE_DIST / "index.html"
    if index.is_file():
        return FileResponse(str(index), headers=_NO_CACHE)
    return FileResponse(str(_FE_DIST / "widget.js"), headers=_NO_CACHE)
