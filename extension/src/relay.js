// Content script (ISOLATED world), injected on demand by sw.js.
//
// Hands the page shim a private MessagePort, then forwards its messages to the
// service worker over a runtime Port and back. Runtime messages are JSON, so
// binary bodies are converted to base64 here; the MessagePort side carries
// real ArrayBuffers.
(() => {
  const NONCE = globalThis.__voittaRelayNonce;
  if (!NONCE || globalThis.__voittaRelayUp) return;
  globalThis.__voittaRelayUp = true;

  const V = "voitta-ext";
  const channel = new MessageChannel();
  const page = channel.port1;
  const openSockets = new Set();
  const pendingFetches = new Set();
  let port = null;

  function bufToB64(buf) {
    const bytes = new Uint8Array(buf);
    let s = "";
    for (let i = 0; i < bytes.length; i += 0x8000) {
      s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    }
    return btoa(s);
  }
  function b64ToBuf(b64) {
    const s = atob(b64);
    const bytes = new Uint8Array(s.length);
    for (let i = 0; i < s.length; i++) bytes[i] = s.charCodeAt(i);
    return bytes.buffer;
  }

  // The worker can be restarted by Chrome. Fail what was in flight so the
  // widget's socket.io client reconnects, and open a new Port on next use.
  function onDisconnect() {
    port = null;
    for (const id of openSockets) {
      page.postMessage({ v: V, t: "ws-event", id, event: "close", code: 1006,
        reason: "voitta: extension restarted", wasClean: false });
    }
    openSockets.clear();
    for (const id of pendingFetches) {
      page.postMessage({ v: V, t: "fetch-res", id, error: "voitta: extension restarted" });
    }
    pendingFetches.clear();
  }

  function worker() {
    if (port) return port;
    port = chrome.runtime.connect({ name: "voitta-tunnel" });
    port.onMessage.addListener(fromWorker);
    port.onDisconnect.addListener(onDisconnect);
    return port;
  }

  function fromWorker(d) {
    const out = { ...d, v: V };
    const transfer = [];
    if (d.b64 != null) {
      const buf = b64ToBuf(d.b64);
      delete out.b64;
      if (d.t === "fetch-res") out.body = buf;
      else { out.data = buf; out.isBinary = true; }
      transfer.push(buf);
    }
    if (d.t === "fetch-res") pendingFetches.delete(d.id);
    if (d.t === "ws-event" && d.event === "close") openSockets.delete(d.id);
    page.postMessage(out, transfer);
  }

  page.onmessage = (e) => {
    const d = e.data;
    if (!d || d.v !== V) return;
    const out = { t: d.t, id: d.id };
    if (d.t === "fetch") {
      Object.assign(out, { url: d.url, method: d.method, headers: d.headers });
      if (d.body) out.b64 = bufToB64(d.body);
      pendingFetches.add(d.id);
    } else if (d.t === "ws-open") {
      Object.assign(out, { url: d.url, protocols: d.protocols });
      openSockets.add(d.id);
    } else if (d.t === "ws-send") {
      if (d.data instanceof ArrayBuffer) out.b64 = bufToB64(d.data);
      else out.data = d.data;
    } else if (d.t === "ws-close") {
      Object.assign(out, { code: d.code, reason: d.reason });
    } else {
      return;
    }
    worker().postMessage(out);
  };

  window.postMessage({ v: V, t: "port", nonce: NONCE }, "*", [channel.port2]);
})();
