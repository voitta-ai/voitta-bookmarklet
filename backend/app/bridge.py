"""Hardened-site bridge — popup relay + page-side network shims.

Why this exists
---------------
On ordinary pages the bookmarklet injects ``<script src=…/widget.js>`` and
the widget talks to the backend directly over https. On pages with a strict
Content-Security-Policy (Salesforce Lightning is the motivating case) two
directives kill that:

  * ``script-src`` forbids loading a ``<script>`` from 127.0.0.1, and
  * ``connect-src`` forbids the page opening *any* connection (fetch / XHR /
    WebSocket) to 127.0.0.1 — so even an eval'd widget can't reach the backend.

CSP, however, is per-document and governs *network* egress. It does NOT
govern ``window.open`` (a top-level navigation, not ``frame-src``) nor
``window.postMessage`` (in-process messaging, not ``connect-src``). So the
widget is bridged through a popup window served from the backend's own
plain-http origin (``PLAINTEXT_PORT``):

    Salesforce tab (CSP-locked)            Popup = http://127.0.0.1:PLAINTEXT (no CSP)
    ─────────────────────────────         ─────────────────────────────────────────
    bookmarklet → window.open(popup)
    widget (eval'd) runs here              relay.js runs here
      fetch/WebSocket SHIMMED ──postMessage──▶ real fetch / WebSocket (same-origin → free)
                              ◀──postMessage──   results / frames

The popup is the only context that actually touches the backend; the locked
page only ever calls ``window.open`` and ``postMessage``, both CSP-exempt.

Pieces
------
* ``GET /bridge``        → ``_BRIDGE_HTML``  (the popup document; loads relay)
* ``GET /bridge/relay.js`` → ``_RELAY_JS``  (runs in the popup)
* ``GET /bridge/boot.js``  → ``_BOOT_JS``   (runs in the locked page; installs
                                              the fetch/WebSocket shims, then
                                              loads the widget)

The locked page never fetches ``boot.js`` itself (``connect-src`` blocks it);
the popup fetches it same-origin and hands the source to the opener over
``postMessage`` for the page to ``eval`` (``'unsafe-eval'`` is permitted, and
this CSP sets no ``require-trusted-types-for``).

This module is plain-http only in practice but is mounted on the shared ASGI
app, so the routes exist on both listeners.
"""

from __future__ import annotations

import html
import json
import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

router = APIRouter()

_JS_HEADERS = {"Cache-Control": "no-store"}


# ───────────────────────────────────────────────────────────────────────────
# Popup document
# ───────────────────────────────────────────────────────────────────────────
_BRIDGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Voitta bridge</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  html,body{margin:0;height:100%;font:13px/1.5 -apple-system,system-ui,sans-serif;
    background:#0b1220;color:#cbd5e1}
  .wrap{display:flex;flex-direction:column;align-items:center;justify-content:center;
    height:100%;text-align:center;padding:24px;box-sizing:border-box}
  .dot{width:10px;height:10px;border-radius:50%;background:#f59e0b;margin-bottom:14px}
  .dot.ok{background:#22c55e}
  h1{font-size:15px;margin:0 0 6px;color:#e2e8f0}
  p{margin:4px 0;opacity:.75}
  code{color:#93c5fd}
</style>
</head>
<body>
  <div class="wrap">
    <div class="dot" id="dot"></div>
    <h1>Voitta bridge</h1>
    <p id="status">Connecting to the page…</p>
    <p style="margin-top:14px;font-size:11px;opacity:.5">
      Keep this window open while you use Voitta.<br>Closing it disconnects the assistant.
    </p>
  </div>
  <script src="/bridge/relay.js"></script>
</body>
</html>
"""


# ───────────────────────────────────────────────────────────────────────────
# Relay — runs in the popup (backend origin, no CSP). Executes the locked
# page's network requests same-origin and relays results back.
# ───────────────────────────────────────────────────────────────────────────
_RELAY_JS = r"""(function () {
  "use strict";
  var V = "voitta-bridge";
  var opener = window.opener;
  var statusEl = document.getElementById("status");
  var dotEl = document.getElementById("dot");
  function setStatus(t, ok) {
    if (statusEl) statusEl.textContent = t;
    if (ok && dotEl) dotEl.classList.add("ok");
  }
  if (!opener) {
    setStatus("No opener window — open Voitta from the bookmarklet.");
    return;
  }

  var openerOrigin = null;       // learned from the 'hello' handshake; then pinned
  var sockets = {};              // id -> real WebSocket
  // Verbose frame logging — off unless localStorage.vbridge_debug === "1"
  // (toggleable in the popup's console without a rebuild).
  var DBG = false;
  try { DBG = localStorage.getItem("vbridge_debug") === "1"; } catch (_) {}
  function dpreview(x) {
    try { return typeof x === "string" ? x.slice(0, 240)
      : "[binary " + (x && x.byteLength) + "B]"; } catch (_) { return "?"; }
  }

  function toOpener(msg, transfer) {
    try { opener.postMessage(msg, openerOrigin || "*", transfer || []); }
    catch (e) { /* opener gone */ }
  }

  window.addEventListener("message", function (e) {
    if (e.source !== opener) return;
    var d = e.data;
    if (!d || d.v !== V) return;

    if (d.t === "hello") {
      openerOrigin = e.origin;       // pin to the page that opened us
      setStatus("Connected — loading assistant…", true);
      sendBoot();
      return;
    }
    // Nothing is relayed before the handshake, and after it only messages
    // from the pinned origin are accepted.
    if (!openerOrigin || e.origin !== openerOrigin) return;

    if (d.t === "fetch") handleFetch(d);
    else if (d.t === "ws-open") handleWsOpen(d);
    else if (d.t === "ws-send") {
      var s = sockets[d.id];
      if (DBG) console.log("[vbridge] popup→server", dpreview(d.data));
      if (s && s.readyState === 1) { try { s.send(d.data); } catch (_) {} }
    } else if (d.t === "ws-close") {
      var s2 = sockets[d.id];
      if (s2) { try { s2.close(d.code, d.reason); } catch (_) {} }
    }
  });

  // Every relayed request is tagged with the opener's browser-verified
  // origin (X-Voitta-Via / ?voitta_via=) so the backend applies that site's
  // approval, not ours (app.services.site_check). Only same-origin targets.
  function viaUrl(raw) {
    var u = new URL(raw, location.href);
    if (u.origin !== location.origin) return null;
    u.searchParams.delete("voitta_via");
    return u;
  }
  function viaHeaders(h) {
    var out = {};
    if (h) Object.keys(h).forEach(function (k) {
      if (k.toLowerCase() !== "x-voitta-via") out[k] = h[k];
    });
    out["X-Voitta-Via"] = openerOrigin;
    return out;
  }

  function handleFetch(d) {
    var target = viaUrl(d.url);
    if (!target) { toOpener({ v: V, t: "fetch-res", id: d.id, error: "bridge: cross-origin target" }); return; }
    var opts = { method: d.method || "GET", headers: viaHeaders(d.headers), credentials: "include" };
    if (d.body != null && opts.method !== "GET" && opts.method !== "HEAD") {
      opts.body = d.body;            // ArrayBuffer (or null)
    }
    fetch(target.href, opts).then(function (r) {
      return r.arrayBuffer().then(function (buf) {
        var headers = {};
        r.headers.forEach(function (v, k) { headers[k] = v; });
        toOpener({
          v: V, t: "fetch-res", id: d.id, ok: r.ok, status: r.status,
          statusText: r.statusText, headers: headers, url: r.url, body: buf,
        }, [buf]);
      });
    }).catch(function (err) {
      toOpener({ v: V, t: "fetch-res", id: d.id, error: String(err && err.message || err) });
    });
  }

  function handleWsOpen(d) {
    var ws, wu;
    try {
      wu = new URL(d.url, location.href);
      var same = (wu.protocol === "wss:" ? "https:" : "http:") + "//" + wu.host;
      if (same !== location.origin) throw new Error("cross-origin socket");
      wu.searchParams.set("voitta_via", openerOrigin);
      ws = d.protocols ? new WebSocket(wu.href, d.protocols) : new WebSocket(wu.href);
    }
    catch (err) { toOpener({ v: V, t: "ws-event", id: d.id, event: "error" }); return; }
    ws.binaryType = "arraybuffer";
    sockets[d.id] = ws;
    ws.onopen = function () { toOpener({ v: V, t: "ws-event", id: d.id, event: "open" }); };
    ws.onmessage = function (ev) {
      var data = ev.data, isBinary = false, transfer = [];
      if (data instanceof ArrayBuffer) { isBinary = true; transfer = [data]; }
      if (DBG) console.log("[vbridge] server→popup", dpreview(data));
      toOpener({ v: V, t: "ws-event", id: d.id, event: "message", data: data, isBinary: isBinary }, transfer);
    };
    ws.onclose = function (ev) {
      delete sockets[d.id];
      toOpener({ v: V, t: "ws-event", id: d.id, event: "close",
                 code: ev.code, reason: ev.reason, wasClean: ev.wasClean });
    };
    ws.onerror = function () { toOpener({ v: V, t: "ws-event", id: d.id, event: "error" }); };
  }

  function sendBoot() {
    fetch("/bridge/boot.js").then(function (r) { return r.text(); }).then(function (code) {
      toOpener({ v: V, t: "boot", code: code });
    }).catch(function (err) {
      setStatus("Failed to load assistant: " + err);
    });
  }

  // Re-announce on a short timer until the opener completes the handshake,
  // in case the opener's listener wasn't attached when we first loaded.
  var tries = 0;
  (function ping() {
    if (openerOrigin) return;
    opener.postMessage({ v: V, t: "ready" }, "*");
    if (++tries < 40) setTimeout(ping, 100);
  })();
})();
"""


# ───────────────────────────────────────────────────────────────────────────
# Boot — runs in the locked page (eval'd from source the popup delivers).
# Installs window.fetch + window.WebSocket shims that tunnel backend-bound
# traffic through the popup, then loads the widget bundle.
# ───────────────────────────────────────────────────────────────────────────
_BOOT_JS = r"""(function () {
  "use strict";
  // Idempotency: a second bookmarklet click on an already-booted page must
  // not re-install the shims (that would orphan the running widget). The
  // bookmarklet's own guard normally prevents reaching here twice; this is a
  // belt-and-suspenders backstop.
  if (window.__voittaBridgeBooted) return;
  window.__voittaBridgeBooted = true;

  var V = "voitta-bridge";
  var BACKEND = window.__voittaBackendOrigin;          // e.g. http://127.0.0.1:12359
  var popup = window.__voittaBridgePopup;
  var BACKEND_HOST;
  try { BACKEND_HOST = new URL(BACKEND).host; } catch (_) { BACKEND_HOST = ""; }

  // Verbose frame logging — off unless localStorage.vbridge_debug === "1".
  var DBG = false;
  try { DBG = localStorage.getItem("vbridge_debug") === "1"; } catch (_) {}
  function dpreview(x) {
    try { return typeof x === "string" ? x.slice(0, 240)
      : "[binary " + (x && (x.byteLength || x.size)) + "B]"; } catch (_) { return "?"; }
  }
  function isBackend(url) {
    try { return new URL(url, location.href).host === BACKEND_HOST; }
    catch (_) { return false; }
  }
  function toPopup(msg, transfer) { popup.postMessage(msg, BACKEND, transfer || []); }

  var nextId = 1;
  var pendingFetch = {};   // id -> {resolve, reject}
  var wsById = {};         // id -> BridgeWebSocket

  window.addEventListener("message", function (e) {
    if (e.source !== popup) return;
    var d = e.data;
    if (!d || d.v !== V) return;
    if (d.t === "fetch-res") {
      var p = pendingFetch[d.id];
      if (!p) return;
      delete pendingFetch[d.id];
      if (d.error) { p.reject(new TypeError("Bridge fetch failed: " + d.error)); return; }
      var nullBody = (d.status === 204 || d.status === 205 || d.status === 304);
      var resp = new Response(nullBody ? null : (d.body || null), {
        status: d.status, statusText: d.statusText || "", headers: d.headers || {},
      });
      try { Object.defineProperty(resp, "url", { value: d.url, configurable: true }); } catch (_) {}
      p.resolve(resp);
    } else if (d.t === "ws-event") {
      var s = wsById[d.id];
      if (s) s.__emit(d);
    }
  });

  // ---- fetch shim ---------------------------------------------------------
  var realFetch = window.fetch ? window.fetch.bind(window) : null;
  window.fetch = function (input, init) {
    var probe;
    try { probe = (typeof input === "string" || input instanceof URL) ? String(input) : input.url; }
    catch (_) { probe = ""; }
    if (!isBackend(probe)) return realFetch(input, init);

    // Normalise any body shape (string/Blob/FormData/ArrayBuffer) into an
    // ArrayBuffer + headers via the Request constructor — this also folds in
    // multipart boundaries for FormData uploads.
    var req;
    try { req = new Request(input, init); }
    catch (e) { return Promise.reject(e); }
    var headers = {};
    req.headers.forEach(function (v, k) { headers[k] = v; });

    return req.clone().arrayBuffer().then(function (buf) {
      return new Promise(function (resolve, reject) {
        var id = nextId++;
        pendingFetch[id] = { resolve: resolve, reject: reject };
        var hasBody = buf && buf.byteLength > 0;
        toPopup({
          v: V, t: "fetch", id: id, url: req.url, method: req.method,
          headers: headers, body: hasBody ? buf : null,
        }, hasBody ? [buf] : []);
      });
    });
  };

  // ---- WebSocket shim -----------------------------------------------------
  var RealWS = window.WebSocket;
  function BridgeWebSocket(url, protocols) {
    var abs;
    try { abs = new URL(url, location.href); } catch (_) { abs = null; }
    if (!abs || abs.host !== BACKEND_HOST) {
      // Not backend-bound — defer to the real implementation.
      return protocols ? new RealWS(url, protocols) : new RealWS(url);
    }
    this.url = abs.href;
    this.readyState = 0;            // CONNECTING
    this.bufferedAmount = 0;
    this.extensions = "";
    this.protocol = "";
    this.binaryType = "blob";
    this.onopen = this.onclose = this.onmessage = this.onerror = null;
    this.__listeners = {};
    this.__id = nextId++;
    wsById[this.__id] = this;
    toPopup({ v: V, t: "ws-open", id: this.__id, url: abs.href, protocols: protocols || null });
  }
  BridgeWebSocket.CONNECTING = 0;
  BridgeWebSocket.OPEN = 1;
  BridgeWebSocket.CLOSING = 2;
  BridgeWebSocket.CLOSED = 3;

  BridgeWebSocket.prototype.send = function (data) {
    var self = this;
    if (data instanceof Blob) {
      data.arrayBuffer().then(function (b) {
        toPopup({ v: V, t: "ws-send", id: self.__id, data: b, isBinary: true });
      });
      return;
    }
    var isBinary = (data instanceof ArrayBuffer) || ArrayBuffer.isView(data);
    var payload = data;
    if (ArrayBuffer.isView(data)) {
      // Copy out the view's bytes (don't transfer — engine.io reuses buffers).
      payload = data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength);
    }
    if (DBG) console.log("[vbridge] page→popup", dpreview(payload));
    toPopup({ v: V, t: "ws-send", id: this.__id, data: payload, isBinary: isBinary });
  };
  BridgeWebSocket.prototype.close = function (code, reason) {
    if (this.readyState >= 2) return;
    this.readyState = 2;            // CLOSING
    toPopup({ v: V, t: "ws-close", id: this.__id, code: code, reason: reason });
  };
  BridgeWebSocket.prototype.addEventListener = function (type, fn) {
    (this.__listeners[type] = this.__listeners[type] || []).push(fn);
  };
  BridgeWebSocket.prototype.removeEventListener = function (type, fn) {
    var a = this.__listeners[type];
    if (!a) return;
    var i = a.indexOf(fn);
    if (i >= 0) a.splice(i, 1);
  };
  BridgeWebSocket.prototype.dispatchEvent = function () { return true; };
  BridgeWebSocket.prototype.__fire = function (type, ev) {
    var h = this["on" + type];
    if (h) { try { h.call(this, ev); } catch (e) { console.error("[voitta-bridge]", e); } }
    var a = this.__listeners[type];
    if (a) a.slice().forEach(function (fn) {
      try { fn.call(this, ev); } catch (e) { console.error("[voitta-bridge]", e); }
    }, this);
  };
  BridgeWebSocket.prototype.__emit = function (d) {
    if (DBG && d.event === "message") console.log("[vbridge] popup→page", dpreview(d.data));
    if (d.event === "open") {
      this.readyState = 1;
      this.__fire("open", _mkEvent("open"));
    } else if (d.event === "message") {
      var data = d.data;
      if (d.isBinary && data instanceof ArrayBuffer && this.binaryType === "blob") {
        data = new Blob([data]);
      }
      this.__fire("message", _mkMessage(data));
    } else if (d.event === "close") {
      this.readyState = 3;
      delete wsById[this.__id];
      this.__fire("close", _mkClose(d.code, d.reason, d.wasClean));
    } else if (d.event === "error") {
      this.__fire("error", _mkEvent("error"));
    }
  };
  function _mkEvent(type) { try { return new Event(type); } catch (_) { return { type: type }; } }
  function _mkMessage(data) {
    try { return new MessageEvent("message", { data: data }); }
    catch (_) { return { type: "message", data: data }; }
  }
  function _mkClose(code, reason, wasClean) {
    try { return new CloseEvent("close", { code: code, reason: reason, wasClean: wasClean }); }
    catch (_) { return { type: "close", code: code, reason: reason, wasClean: wasClean }; }
  }
  window.WebSocket = BridgeWebSocket;

  // ---- load the widget ----------------------------------------------------
  // Fetched through the (now-shimmed) fetch, so it tunnels via the popup.
  window.fetch(BACKEND + "/widget.js").then(function (r) {
    if (!r.ok) throw new Error("widget.js HTTP " + r.status);
    return r.text();
  }).then(function (src) {
    (0, eval)(src);
  }).catch(function (err) {
    console.error("[voitta-bridge] failed to load widget:", err);
  });
})();
"""


@router.get("/bridge", include_in_schema=False)
async def bridge_page() -> HTMLResponse:
    return HTMLResponse(content=_BRIDGE_HTML, headers={"Cache-Control": "no-store"})


@router.get("/bridge/relay.js", include_in_schema=False)
async def bridge_relay_js() -> Response:
    return Response(content=_RELAY_JS, media_type="application/javascript", headers=_JS_HEADERS)


@router.get("/bridge/boot.js", include_in_schema=False)
async def bridge_boot_js() -> Response:
    return Response(content=_BOOT_JS, media_type="application/javascript", headers=_JS_HEADERS)


# ───────────────────────────────────────────────────────────────────────────
# Bookmarklet copy page — the server equivalent of the tray's "Copy
# bookmarklet" menu items (there's no tray on a headless server). Renders two
# draggable links built from the public origin. The macOS build still uses the
# tray; this page is for server deployments.
# ───────────────────────────────────────────────────────────────────────────

# Template uses unique __TOKEN__ placeholders (not str.format) so the embedded
# CSS/JS braces need no escaping. Each bookmarklet can be installed two ways:
# drag the pill to the bookmarks bar, OR click Copy and paste into a new
# bookmark's URL field (handy when the bookmarks bar is hidden).
_BOOKMARKLETS_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Voitta — your AI assistant on every page</title>
<meta name="description" content="Voitta is an AI assistant you launch on any web page from a bookmark. Drag the bookmarklet to your bar and go.">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@500;600;700&family=Inter:wght@400;500;600;700&display=swap">
<style>
  /* McKinsey-editorial: white canvas, deep-navy serif display, ONE restrained
     blue accent used sparingly (eyebrow tick, one italic phrase, links, step
     numerals). Hairline rules, structured whitespace. No dark hero band, no
     glow gradients, no colored card edges. */
  *, *::before, *::after { box-sizing: border-box; }
  :root {
    --navy: #051C2C; --ink: #0a1f2c; --muted: #54636e; --faint: #8a97a1;
    --accent: #2251ff; --accent-deep: #1330b8;
    --line: #e3e7ec;
    --serif: "Playfair Display", Georgia, "Times New Roman", serif;
    --sans: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  }
  html, body { margin: 0; }
  body { font: 16px/1.65 var(--sans); color: var(--ink); background: #fff; min-height: 100vh;
         overflow-x: hidden; }
  h1, .lede { overflow-wrap: break-word; }
  .wrap { max-width: 1080px; margin: 0 auto; padding: 0 32px; }

  /* nav — white, navy wordmark, hairline underline */
  header.nav { border-bottom: 1px solid var(--line); }
  header.nav .wrap { display: flex; align-items: center; gap: 11px; padding: 22px 32px; }
  header.nav img { width: 24px; height: 24px; border-radius: 5px; }
  header.nav .name { font-weight: 700; font-size: 17px; letter-spacing: 0.12em; color: var(--navy);
                     text-transform: uppercase; }
  header.nav .spacer { flex: 1; }
  header.nav a.ghost { color: var(--muted); text-decoration: none; font-size: 12px; font-weight: 600;
                       letter-spacing: 0.12em; text-transform: uppercase; }
  header.nav a.ghost:hover { color: var(--navy); }

  /* hero — white, navy serif headline, illustration as a bordered exhibit */
  .hero { display: grid; grid-template-columns: 1.05fr 0.95fr; gap: 48px; align-items: center;
          padding: 24px 0 28px; }
  .eyebrow { font-size: 12px; font-weight: 600; letter-spacing: 0.16em; text-transform: uppercase;
             color: var(--accent); }
  .eyebrow::before { content: ""; display: inline-block; width: 24px; height: 1px;
             background: var(--accent); vertical-align: middle; margin-right: 12px; }
  h1 { font-family: var(--serif); font-weight: 600; font-size: 44px; line-height: 1.05;
       letter-spacing: -0.005em; margin: 14px 0 12px; color: var(--navy); }
  h1 .accent { font-style: italic; color: var(--accent); }
  .lede { font-size: 17px; line-height: 1.5; color: var(--muted); margin: 0 0 22px; max-width: 30em; }

  .hero-art { justify-self: center; }
  .hero-art img { display: block; width: 100%; max-width: 420px; height: auto;
                  border: 1px solid var(--line); border-radius: 3px;
                  box-shadow: 0 22px 44px rgba(5,28,44,0.08); }

  /* install cards — white, single hairline border, no colored edges */
  .cards { display: grid; gap: 12px; max-width: 560px; }
  .card { border: 1px solid var(--line); border-radius: 4px; padding: 14px 18px; background: #fff; }
  .card h3 { margin: 0 0 3px; font-size: 15px; font-weight: 700; color: var(--navy); }
  .card .dim { color: var(--faint); font-weight: 400; }
  .card p { margin: 0 0 12px; font-size: 13.5px; color: var(--muted); }
  .actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .bm { display: inline-flex; align-items: center; gap: 9px; padding: 11px 18px; background: var(--navy);
        color: #fff; border-radius: 3px; text-decoration: none; font-weight: 600; font-size: 14px;
        letter-spacing: 0.01em; cursor: grab; user-select: none; }
  .bm:hover { background: #0c3350; }
  .bm:active { cursor: grabbing; }
  .bm .grip { opacity: 0.5; }
  /* secondary install = outline, so the two CTAs differ without a second colour */
  .card.alt .bm { background: #fff; color: var(--navy); border: 1px solid var(--navy); padding: 10px 17px; }
  .card.alt .bm:hover { background: #f3f5f8; }
  .copy { display: inline-flex; align-items: center; gap: 7px; padding: 10px 14px; background: #fff;
          color: var(--navy); border: 1px solid var(--line); border-radius: 3px;
          font: 600 14px var(--sans); cursor: pointer; }
  .copy:hover { border-color: var(--navy); }
  .copy svg { width: 15px; height: 15px; }

  /* steps — exhibit columns: navy top rule, serif numeral, hairline rhythm */
  .steps-section { border-top: 1px solid var(--line); padding: 24px 0 4px; }
  .kicker { font-size: 12px; font-weight: 600; letter-spacing: 0.16em; text-transform: uppercase;
            color: var(--accent); margin-bottom: 20px; }
  .kicker::before { content: ""; display: inline-block; width: 24px; height: 1px;
            background: var(--accent); vertical-align: middle; margin-right: 12px; }
  .steps { display: grid; grid-template-columns: repeat(3, 1fr); gap: 44px; }
  .step { font-size: 13.5px; color: var(--muted); padding-top: 14px; border-top: 2px solid var(--navy); }
  .step .n { font-family: var(--serif); font-size: 30px; font-weight: 600; color: var(--navy);
             line-height: 1; display: block; margin-bottom: 9px; }
  .step b { color: var(--navy); display: block; font-size: 15px; margin-bottom: 4px; font-weight: 600; }

  /* footer — slim navy band closes the page */
  footer.foot { margin-top: 24px; background: var(--navy); color: #9fb1bc; }
  footer.foot .wrap { display: flex; align-items: center; gap: 11px; padding: 18px 32px;
                      font-size: 13px; letter-spacing: 0.02em; }
  footer.foot img { width: 20px; height: 20px; border-radius: 5px; }
  footer.foot .name { color: #fff; font-weight: 700; letter-spacing: 0.12em; text-transform: uppercase; }
  footer.foot .spacer { flex: 1; }
  footer.foot a { color: #cdd9e0; text-decoration: none; }
  footer.foot a:hover { color: #fff; }

  @media (max-width: 860px) {
    .wrap { padding: 0 22px; }
    header.nav .wrap, footer.foot .wrap { padding: 16px 22px; }
    .hero { grid-template-columns: 1fr; gap: 32px; padding: 44px 0 52px; }
    .hero-art { order: -1; }
    .hero-art img { max-width: 320px; }
    h1 { font-size: 34px; }
    .lede { font-size: 16px; }
    .steps { grid-template-columns: 1fr; gap: 30px; }
    .steps-section { padding: 44px 0 4px; }
    footer.foot { margin-top: 44px; }
  }
</style></head>
<body>
  <header class="nav">
    <div class="wrap">
      <img src="/favicon.svg" alt="">
      <span class="name">Voitta</span>
      <span class="spacer"></span>
      <a class="ghost" href="https://voitta.ai">voitta.ai</a>
    </div>
  </header>

  <section class="wrap hero">
    <div>
      <span class="eyebrow">Bookmarklet · works on any site</span>
      <h1>Your AI assistant, <span class="accent">on every page</span>.</h1>
      <p class="lede">Voitta rides along in your browser. Drag a bookmark to your bar, click it on
        any page, and a chat assistant with real compute slides in — no extension, no install.</p>
      <div class="cards">
        <div class="card">
          <h3>Voitta</h3>
          <p>For ordinary pages. Injects the assistant directly.</p>
          <div class="actions">
            <a class="bm" href="__NORMAL_HREF__"><span class="grip">⠿</span>Voitta</a>
            <button class="copy" data-bm="normal" type="button"></button>
          </div>
        </div>
        <div class="card alt">
          <h3>Voitta for Salesforce <span class="dim">· strict CSP</span></h3>
          <p>For hardened pages (Salesforce Lightning, etc.) that block the direct widget. Opens a small
            popup — keep it open while you work.</p>
          <div class="actions">
            <a class="bm" href="__BRIDGE_HREF__"><span class="grip">⠿</span>Voitta (Salesforce)</a>
            <button class="copy" data-bm="bridge" type="button"></button>
          </div>
        </div>
      </div>
    </div>
    <div class="hero-art">
      <img src="/hero.png" alt="Isometric illustration: a web page with the Voitta chat assistant docked alongside it" loading="eager" width="420" height="420">
    </div>
  </section>

  <section class="steps-section">
    <div class="wrap">
      <div class="kicker">Get started in three steps</div>
      <div class="steps">
        <div class="step"><span class="n">1</span><b>Add the bookmark</b>Drag a button above to your bookmarks bar, or Copy &amp; paste it into a new bookmark's URL.</div>
        <div class="step"><span class="n">2</span><b>Open any page</b>Navigate to the site you want help with — Drive, Salesforce, a dashboard, anything.</div>
        <div class="step"><span class="n">3</span><b>Click Voitta</b>The assistant slides in, sees the page, and can run code, search, and build for you.</div>
      </div>
    </div>
  </section>

  <footer class="foot">
    <div class="wrap">
      <img src="/favicon.svg" alt="">
      <span class="name">Voitta</span>
      <span class="spacer"></span>
      <a href="https://voitta.ai">voitta.ai</a>
    </div>
  </footer>

  <script>
    var BM = __BM_JSON__;
    var COPY_SVG = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><rect x="5.5" y="5.5" width="8" height="8" rx="1.5"/><path d="M3.5 10.5h-1a1 1 0 0 1-1-1v-7a1 1 0 0 1 1-1h7a1 1 0 0 1 1 1v1"/></svg>';
    document.querySelectorAll('.copy').forEach(function (btn) {
      btn.innerHTML = COPY_SVG + '<span>Copy</span>';
      btn.addEventListener('click', function () {
        var text = BM[btn.getAttribute('data-bm')];
        var label = btn.querySelector('span');
        var done = function () { label.textContent = 'Copied!'; setTimeout(function () { label.textContent = 'Copy'; }, 1400); };
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(done, function () { fallback(text); done(); });
        } else { fallback(text); done(); }
      });
    });
    function fallback(text) {
      var ta = document.createElement('textarea'); ta.value = text;
      document.body.appendChild(ta); ta.select();
      try { document.execCommand('copy'); } catch (e) {}
      document.body.removeChild(ta);
    }
  </script>
</body></html>
"""


def _public_origin(request: Request) -> str:
    """The origin the bookmarklets point at: VOITTA_PUBLIC_BASE_URL when set
    (server deployments behind a reverse proxy), else the request's own
    scheme+host so the page still works when hit directly."""
    base = (os.getenv("VOITTA_PUBLIC_BASE_URL") or "").rstrip("/")
    if base:
        return base
    return str(request.base_url).rstrip("/")


def render_bookmarklets(request: Request) -> str:
    """Build the bookmarklets HTML for the current public origin. Used by both
    GET /bookmarklets and (in server mode) the site root."""
    from app.bookmarklets import bridge_bookmarklet, normal_bookmarklet

    origin = _public_origin(request)
    normal = normal_bookmarklet(origin)
    bridge = bridge_bookmarklet(origin)
    # href: HTML-attribute-escaped (the browser decodes it back to the real
    # javascript: URL on drag). Copy: JSON-embedded so the exact string reaches
    # the clipboard; guard the </script> sequence just in case.
    bm_json = json.dumps({"normal": normal, "bridge": bridge}).replace("</", "<\\/")
    return (
        _BOOKMARKLETS_PAGE
        .replace("__NORMAL_HREF__", html.escape(normal, quote=True))
        .replace("__BRIDGE_HREF__", html.escape(bridge, quote=True))
        .replace("__ORIGIN__", html.escape(origin))
        .replace("__BM_JSON__", bm_json)
    )


@router.get("/bookmarklets", include_in_schema=False)
async def bookmarklets_page(request: Request) -> HTMLResponse:
    return HTMLResponse(content=render_bookmarklets(request), headers={"Cache-Control": "no-store"})
