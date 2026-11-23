// Service worker: injects the widget into a tab and carries the widget's
// backend traffic (fetch + WebSocket) for it.
//
// Why the traffic comes here: on a strict-CSP page (`connect-src 'self'`) the
// widget cannot open a connection to the backend from the page, and the
// bookmarklet bridge needs `'unsafe-eval'` to boot. Scripts injected by
// chrome.scripting are not subject to the page's CSP, and this worker's
// requests are governed by the extension's host permissions, not by the page.
//
//   page (MAIN world)          content script (ISOLATED)        this worker
//   widget.js + page-shim.js ── MessagePort ──▶ relay.js ── runtime Port ──▶ fetch / WebSocket ──▶ backend
//
// Every relayed request is tagged with the tab's browser-verified origin
// (port.sender.origin) in X-Voitta-Via / ?voitta_via=, the same tag the
// bookmarklet bridge uses, so the backend applies that site's approval
// (app.services.site_check trusts the tag only from Voitta's own extension ID).

import { getBackend, bufToB64, b64ToBuf } from "./config.js";

const TUNNEL = "voitta-tunnel";

// ── launch ───────────────────────────────────────────────────────────────

async function inject(tabId, world, opts) {
  const res = await chrome.scripting.executeScript({ target: { tabId }, world, ...opts });
  return res[0]?.result;
}

async function launch(tabId) {
  const state = await inject(tabId, "MAIN", {
    func: () =>
      window.__voittaExt ? (window.__voittaExt.ready ? "booted" : "partial")
        : document.getElementById("voitta-bookmarklet-host") ? "bookmarklet"
          : "fresh",
  });
  if (state === "partial") throw new Error("an earlier launch failed part-way; reload the page and try again");
  // A page keeps the widget it loaded; a second copy would not replace it.
  if (state !== "fresh") return { ok: true, state };

  const backend = await getBackend();
  const nonce = crypto.randomUUID();
  // Order matters: the page shim must be listening before the relay posts its
  // port, and both must exist before the widget makes its first request (the
  // shim queues requests until the port arrives).
  await inject(tabId, "MAIN", {
    func: (b, n) => { window.__voittaExt = { backend: b, nonce: n }; },
    args: [backend, nonce],
  });
  await inject(tabId, "MAIN", { files: ["src/page-shim.js"] });
  await inject(tabId, "ISOLATED", {
    func: (n) => { globalThis.__voittaRelayNonce = n; },
    args: [nonce],
  });
  await inject(tabId, "ISOLATED", { files: ["src/relay.js"] });
  await inject(tabId, "MAIN", { files: ["vendor/widget.js"] });
  await inject(tabId, "MAIN", { func: () => { window.__voittaExt.ready = true; } });
  return { ok: true, state: "launched" };
}

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  // Only the toolbar popup may launch. Content scripts also send as this
  // extension, but their sender.url is the page's.
  if (sender.id !== chrome.runtime.id || sender.url !== chrome.runtime.getURL("src/popup.html")
      || msg?.t !== "launch") return;
  launch(msg.tabId).then(reply, (err) => reply({ ok: false, error: String(err?.message || err) }));
  return true;
});

// ── tunnel ───────────────────────────────────────────────────────────────

chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== TUNNEL) return;
  const site = port.sender?.origin;
  // Top frame of a tab, http(s) only: the relay is never injected anywhere else.
  if (!port.sender?.tab || port.sender.frameId !== 0 || !/^https?:\/\//.test(site || "")) {
    port.disconnect();
    return;
  }

  const sockets = new Map(); // id -> WebSocket
  const backendP = getBackend();
  const send = (m) => { try { port.postMessage(m); } catch { /* relay gone */ } };

  port.onMessage.addListener(async (d) => {
    const backend = await backendP;
    if (d.t === "exec") { runUserScript(d, port.sender, send); return; }
    if (d.t === "fetch") relayFetch(d, backend, site, send);
    else if (d.t === "ws-open") relayWsOpen(d, backend, site, send, sockets);
    else if (d.t === "ws-send") {
      const ws = sockets.get(d.id);
      if (ws && ws.readyState === WebSocket.OPEN) {
        try { ws.send(d.b64 != null ? b64ToBuf(d.b64) : d.data); } catch { /* closing */ }
      }
    } else if (d.t === "ws-close") {
      const ws = sockets.get(d.id);
      if (ws) { try { ws.close(d.code, d.reason); } catch { /* bad code */ ws.close(); } }
    }
  });

  port.onDisconnect.addListener(() => {
    for (const ws of sockets.values()) { try { ws.close(); } catch { /* already closed */ } }
    sockets.clear();
  });
});

function backendUrl(raw, backend) {
  const u = new URL(raw, backend);
  const scheme = u.protocol === "wss:" ? "https:" : u.protocol === "ws:" ? "http:" : u.protocol;
  if (`${scheme}//${u.host}` !== backend) return null;
  u.searchParams.delete("voitta_via");
  return u;
}

async function relayFetch(d, backend, site, send) {
  const target = backendUrl(d.url, backend);
  if (!target) {
    send({ t: "fetch-res", id: d.id, error: "voitta: request is not for the Voitta backend" });
    return;
  }
  const headers = new Headers();
  for (const [k, v] of Object.entries(d.headers || {})) {
    if (k.toLowerCase() !== "x-voitta-via") headers.set(k, v);
  }
  headers.set("X-Voitta-Via", site);
  const init = { method: d.method || "GET", headers, credentials: "include" };
  if (d.b64 != null && init.method !== "GET" && init.method !== "HEAD") init.body = b64ToBuf(d.b64);
  try {
    const r = await fetch(target.href, init);
    const buf = await r.arrayBuffer();
    const h = {};
    r.headers.forEach((v, k) => { h[k] = v; });
    send({
      t: "fetch-res", id: d.id, ok: r.ok, status: r.status, statusText: r.statusText,
      headers: h, url: r.url, b64: bufToB64(buf),
    });
  } catch (err) {
    send({ t: "fetch-res", id: d.id, error: String(err?.message || err) });
  }
}

function relayWsOpen(d, backend, site, send, sockets) {
  let ws;
  try {
    const u = backendUrl(d.url, backend);
    if (!u) throw new Error("not the Voitta backend");
    u.protocol = u.protocol === "https:" || u.protocol === "wss:" ? "wss:" : "ws:";
    u.searchParams.set("voitta_via", site);
    ws = d.protocols ? new WebSocket(u.href, d.protocols) : new WebSocket(u.href);
  } catch {
    send({ t: "ws-event", id: d.id, event: "error" });
    send({ t: "ws-event", id: d.id, event: "close", code: 1006, reason: "", wasClean: false });
    return;
  }
  ws.binaryType = "arraybuffer";
  sockets.set(d.id, ws);
  ws.onopen = () => send({ t: "ws-event", id: d.id, event: "open", protocol: ws.protocol });
  ws.onmessage = (ev) => {
    if (ev.data instanceof ArrayBuffer) send({ t: "ws-event", id: d.id, event: "message", b64: bufToB64(ev.data) });
    else send({ t: "ws-event", id: d.id, event: "message", data: ev.data });
  };
  ws.onerror = () => send({ t: "ws-event", id: d.id, event: "error" });
  ws.onclose = (ev) => {
    sockets.delete(d.id);
    send({ t: "ws-event", id: d.id, event: "close", code: ev.code, reason: ev.reason, wasClean: ev.wasClean });
  };
}

// ── browser_eval on pages that forbid eval ───────────────────────────────
//
// chrome.userScripts runs a string of code in a USER_SCRIPT world: its own JS
// globals, the page's DOM, and a CSP set here rather than by the page. Chrome
// only allows it once the user turns on "Allow User Scripts" for this
// extension. The script runs in the very document whose relay asked
// (documentIds), never in another tab or after a navigation.

const WORLD_CSP = "script-src 'self' 'unsafe-eval'; object-src 'none'";
let worldReady = null;

function userScriptsAvailable() {
  try {
    chrome.userScripts.getScripts(); // throws while the user hasn't allowed user scripts
    return true;
  } catch {
    return false;
  }
}

// Runs in the user-script world. Mirrors evalJs in frontend/src/lib/primitives.ts:
// async body, console capture, timeout, JSON-safe result. The agent's script is
// embedded as a JSON string literal, never spliced into this source.
function userScriptSource(js, awaitMs) {
  return `(async () => {
  const logs = [];
  const safe = (v) => { try { JSON.stringify(v); return v; } catch { try { return String(v); } catch { return "[unstringifiable]"; } } };
  const orig = { log: console.log, warn: console.warn, error: console.error };
  for (const level of ["log", "warn", "error"]) {
    console[level] = (...a) => { try { logs.push({ level, args: a.map(safe) }); } catch {} try { orig[level].apply(console, a); } catch {} };
  }
  const t0 = performance.now();
  try {
    // Compiled here, where WORLD_CSP allows it: a syntax error comes back as eval_threw
    // with its message, and the script can't break out of this wrapper.
    const AsyncFunction = (async () => {}).constructor;
    const body = new AsyncFunction(${JSON.stringify(js)});
    const result = await Promise.race([
      body(),
      new Promise((_, reject) => setTimeout(() => reject(new Error("eval timed out after ${awaitMs}ms")), ${awaitMs})),
    ]);
    return { ok: true, result: safe(result), logs, ms: Math.round(performance.now() - t0), world: "user_script" };
  } catch (err) {
    return { ok: false, error: "eval_threw", message: err instanceof Error ? err.message : String(err),
             stack: err instanceof Error ? err.stack : undefined, logs, ms: Math.round(performance.now() - t0), world: "user_script" };
  } finally {
    Object.assign(console, orig);
  }
})()`;
}

async function runUserScript(d, sender, send) {
  const reply = (envelope) => send({ t: "exec-res", id: d.id, envelope });
  if (!userScriptsAvailable()) {
    reply({ ok: false, error: "user_scripts_disabled",
      message: "This page forbids eval, so browser_eval needs the Voitta extension's " +
        "'Allow User Scripts' switch: chrome://extensions → Voitta → Details. Turn it on and retry." });
    return;
  }
  const awaitMs = Math.max(100, Math.min(120000, Number(d.awaitMs) || 30000));
  const code = userScriptSource(d.js, awaitMs);
  try {
    worldReady ??= chrome.userScripts.configureWorld({ csp: WORLD_CSP, messaging: false });
    await worldReady;
    const [res] = await chrome.userScripts.execute({
      target: { tabId: sender.tab.id, documentIds: [sender.documentId] },
      js: [{ code }],
      world: "USER_SCRIPT",
      injectImmediately: true,
    });
    if (res?.error) reply({ ok: false, error: "eval_threw", message: res.error, logs: [], ms: 0 });
    else reply(res?.result ?? { ok: false, error: "transport", message: "the script returned nothing" });
  } catch (err) {
    worldReady = null;
    reply({ ok: false, error: "transport", message: String(err?.message || err) });
  }
}
