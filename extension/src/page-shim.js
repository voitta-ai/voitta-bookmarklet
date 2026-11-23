// Page shim (MAIN world), injected by sw.js before widget.js.
//
// Replaces window.fetch and window.WebSocket for requests to the Voitta
// backend only, carrying them over a MessagePort to relay.js. Everything else
// goes to the page's own implementations untouched.
//
// Adapted from the bookmarklet bridge's boot shim (backend/app/bridge.py,
// _BOOT_JS); only the transport differs (a private MessagePort instead of
// postMessage to a popup). TODO: share one implementation between the two.
(function () {
  "use strict";
  var cfg = window.__voittaExt;
  if (!cfg || cfg.shim) return;
  cfg.shim = true;

  var V = "voitta-ext";
  var BACKEND = cfg.backend;
  var BACKEND_HOST = new URL(BACKEND).host;

  // What widget.js reads at load: the backend origin (it has no
  // document.currentScript here), and bridge mode, which pins socket.io to the
  // WebSocket transport and loads images as blob: URLs.
  window.__voittaBackendOrigin = BACKEND;
  window.__voittaBridge = true;

  var port = null;
  var queue = [];
  var nextId = 1;
  var pendingFetch = {};
  var pendingExec = {};
  var wsById = {};

  function send(msg, transfer) {
    msg.v = V;
    if (port) port.postMessage(msg, transfer || []);
    else queue.push([msg, transfer || []]);
  }

  function onPortMessage(e) {
    var d = e.data;
    if (!d || d.v !== V) return;
    if (d.t === "fetch-res") {
      var p = pendingFetch[d.id];
      if (!p) return;
      delete pendingFetch[d.id];
      if (d.error) { p.reject(new TypeError("Voitta fetch failed: " + d.error)); return; }
      var nullBody = d.status === 204 || d.status === 205 || d.status === 304;
      var resp = new Response(nullBody ? null : (d.body || null), {
        status: d.status, statusText: d.statusText || "", headers: d.headers || {},
      });
      try { Object.defineProperty(resp, "url", { value: d.url, configurable: true }); } catch (_) {}
      p.resolve(resp);
    } else if (d.t === "ws-event") {
      var s = wsById[d.id];
      if (s) s.__emit(d);
    } else if (d.t === "exec-res") {
      var x = pendingExec[d.id];
      if (!x) return;
      delete pendingExec[d.id];
      x(d.envelope || { ok: false, error: "transport", message: d.error || "no result" });
    }
  }

  window.addEventListener("message", function onHandshake(e) {
    var d = e.data;
    if (e.source !== window || !d || d.v !== V || d.t !== "port" || d.nonce !== cfg.nonce || !e.ports[0]) return;
    window.removeEventListener("message", onHandshake);
    port = e.ports[0];
    port.onmessage = onPortMessage;
    var q = queue;
    queue = [];
    q.forEach(function (m) { port.postMessage(m[0], m[1]); });
  });

  function isBackend(url) {
    try { return new URL(url, location.href).host === BACKEND_HOST; } catch (_) { return false; }
  }

  // ---- exec ---------------------------------------------------------------
  // browser_eval on a page whose CSP forbids eval (frontend/src/lib/primitives.ts):
  // the worker runs the script with chrome.userScripts.execute and resolves to
  // evalJs's envelope, {ok, result, logs, ms} or {ok: false, error, message, ...}.
  cfg.exec = function (js, awaitMs) {
    return new Promise(function (resolve) {
      var id = nextId++;
      pendingExec[id] = resolve;
      send({ t: "exec", id: id, js: String(js), awaitMs: Number(awaitMs) || 30000 });
    });
  };

  // ---- fetch --------------------------------------------------------------
  var realFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    var probe;
    try { probe = (typeof input === "string" || input instanceof URL) ? String(input) : input.url; }
    catch (_) { probe = ""; }
    if (!isBackend(probe)) return realFetch(input, init);

    var req;
    try { req = new Request(input, init); } catch (e) { return Promise.reject(e); }
    var headers = {};
    req.headers.forEach(function (v, k) { headers[k] = v; });
    return req.clone().arrayBuffer().then(function (buf) {
      return new Promise(function (resolve, reject) {
        var id = nextId++;
        pendingFetch[id] = { resolve: resolve, reject: reject };
        var hasBody = buf && buf.byteLength > 0;
        send({ t: "fetch", id: id, url: req.url, method: req.method, headers: headers,
               body: hasBody ? buf : null }, hasBody ? [buf] : []);
      });
    });
  };

  // ---- WebSocket ----------------------------------------------------------
  var RealWS = window.WebSocket;
  function VoittaWebSocket(url, protocols) {
    var abs;
    try { abs = new URL(url, location.href); } catch (_) { abs = null; }
    if (!abs || abs.host !== BACKEND_HOST) {
      return protocols ? new RealWS(url, protocols) : new RealWS(url);
    }
    this.url = abs.href;
    this.readyState = 0;
    this.bufferedAmount = 0;
    this.extensions = "";
    this.protocol = "";
    this.binaryType = "blob";
    this.onopen = this.onclose = this.onmessage = this.onerror = null;
    this.__listeners = {};
    this.__id = nextId++;
    wsById[this.__id] = this;
    send({ t: "ws-open", id: this.__id, url: abs.href, protocols: protocols || null });
  }
  VoittaWebSocket.CONNECTING = VoittaWebSocket.prototype.CONNECTING = 0;
  VoittaWebSocket.OPEN = VoittaWebSocket.prototype.OPEN = 1;
  VoittaWebSocket.CLOSING = VoittaWebSocket.prototype.CLOSING = 2;
  VoittaWebSocket.CLOSED = VoittaWebSocket.prototype.CLOSED = 3;

  VoittaWebSocket.prototype.send = function (data) {
    var id = this.__id;
    if (this.readyState !== 1) throw new DOMException("WebSocket is not open", "InvalidStateError");
    if (data instanceof Blob) {
      data.arrayBuffer().then(function (b) { send({ t: "ws-send", id: id, data: b }, [b]); });
      return;
    }
    if (ArrayBuffer.isView(data)) {
      // Copy, don't transfer: engine.io reuses its buffers.
      send({ t: "ws-send", id: id, data: data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength) });
      return;
    }
    if (data instanceof ArrayBuffer) { send({ t: "ws-send", id: id, data: data.slice(0) }); return; }
    send({ t: "ws-send", id: id, data: String(data) });
  };
  VoittaWebSocket.prototype.close = function (code, reason) {
    if (this.readyState >= 2) return;
    this.readyState = 2;
    send({ t: "ws-close", id: this.__id, code: code, reason: reason });
  };
  VoittaWebSocket.prototype.addEventListener = function (type, fn) {
    (this.__listeners[type] = this.__listeners[type] || []).push(fn);
  };
  VoittaWebSocket.prototype.removeEventListener = function (type, fn) {
    var a = this.__listeners[type];
    var i = a ? a.indexOf(fn) : -1;
    if (i >= 0) a.splice(i, 1);
  };
  VoittaWebSocket.prototype.dispatchEvent = function () { return true; };
  VoittaWebSocket.prototype.__fire = function (type, ev) {
    var self = this;
    var h = this["on" + type];
    if (h) { try { h.call(this, ev); } catch (e) { console.error("[voitta-ext]", e); } }
    (this.__listeners[type] || []).slice().forEach(function (fn) {
      try { fn.call(self, ev); } catch (e) { console.error("[voitta-ext]", e); }
    });
  };
  VoittaWebSocket.prototype.__emit = function (d) {
    if (d.event === "open") {
      this.readyState = 1;
      this.protocol = d.protocol || "";
      this.__fire("open", new Event("open"));
    } else if (d.event === "message") {
      var data = d.data;
      if (d.isBinary && this.binaryType === "blob") data = new Blob([data]);
      this.__fire("message", new MessageEvent("message", { data: data }));
    } else if (d.event === "close") {
      if (this.readyState === 3) return;
      this.readyState = 3;
      delete wsById[this.__id];
      this.__fire("close", new CloseEvent("close", { code: d.code, reason: d.reason, wasClean: d.wasClean }));
    } else if (d.event === "error") {
      this.__fire("error", new Event("error"));
    }
  };
  window.WebSocket = VoittaWebSocket;
})();
