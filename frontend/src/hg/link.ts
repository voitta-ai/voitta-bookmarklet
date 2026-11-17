// The panel's connection to hg.voitta.ai: API calls for this key arrive here and run in this
// StockTrak tab. Only Hedgehog primitives are ever run (the server only sends those, and this
// side refuses anything else too).

import { PrimitiveError } from "../lib/bridge";
import { primitives } from "../lib/primitives";

export type LinkState = "connecting" | "connected" | "offline" | "bad_key";

interface Handlers {
  onState: (s: LinkState, tabId?: number) => void;
  onBadKey: () => void; // key unknown/revoked: the panel starts fresh
}

const BACKOFF_S = [1, 2, 5, 10, 30];
const PING_MS = 25_000;
const ACTIVE_THROTTLE_MS = 5_000;

export class HgLink {
  private ws: WebSocket | null = null;
  private tries = 0;
  private timer = 0;
  private ping = 0;
  private lastActive = 0;
  private stopped = false;
  private key: string;

  constructor(private base: string, key: string, private h: Handlers) {
    this.key = key;
    const onActive = () => this.active();
    window.addEventListener("focus", onActive);
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") onActive();
    });
    document.addEventListener("pointerdown", onActive, { capture: true, passive: true });
  }

  start(): void {
    this.stopped = false;
    this.open();
  }

  /** Reconnect with another key (after regenerate / a fresh key). */
  useKey(key: string): void {
    this.key = key;
    this.restart();
  }

  restart(): void {
    this.tries = 0;
    try {
      this.ws?.close();
    } catch {
      /* already closed */
    }
    this.ws = null;
    clearTimeout(this.timer);
    this.open();
  }

  /** This tab is in use: API calls should come here (throttled). */
  active(force = false): void {
    const now = Date.now();
    if (!force && now - this.lastActive < ACTIVE_THROTTLE_MS) return;
    this.lastActive = now;
    this.send({ t: "active", page: location.href });
  }

  private send(msg: unknown): void {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(msg));
  }

  private open(): void {
    if (this.stopped) return;
    this.h.onState("connecting");
    const url = this.base.replace(/^http/, "ws") + "/hg/link";
    let ws: WebSocket;
    try {
      ws = new WebSocket(url);
    } catch {
      this.retry();
      return;
    }
    this.ws = ws;
    ws.onopen = () => ws.send(JSON.stringify({ t: "hello", key: this.key, page: location.href }));
    ws.onmessage = (ev) => void this.onMessage(ws, ev.data);
    ws.onclose = (ev) => {
      if (this.ws !== ws) return;
      clearInterval(this.ping);
      this.ws = null;
      if (ev.code === 4401) return; // bad key: handled on the "bye" message
      this.h.onState("offline");
      this.retry();
    };
  }

  private retry(): void {
    clearTimeout(this.timer);
    const s = BACKOFF_S[Math.min(this.tries++, BACKOFF_S.length - 1)];
    this.timer = window.setTimeout(() => this.open(), s * 1000);
  }

  private async onMessage(ws: WebSocket, raw: unknown): Promise<void> {
    let msg: { t?: string; id?: number; name?: string; args?: Record<string, unknown>; tab?: number; reason?: string };
    try {
      msg = JSON.parse(String(raw));
    } catch {
      return;
    }
    if (msg.t === "ready") {
      this.tries = 0;
      this.h.onState("connected", msg.tab);
      this.active(true); // the tab you just opened the panel in takes the calls
      clearInterval(this.ping);
      this.ping = window.setInterval(() => this.send({ t: "ping" }), PING_MS);
      return;
    }
    if (msg.t === "bye") {
      if (msg.reason === "bad_key") {
        this.h.onState("bad_key");
        this.h.onBadKey();
      }
      // key_regenerated: the panel already holds the new key and reconnects with it
      return;
    }
    if (msg.t !== "call" || typeof msg.id !== "number") return;
    const name = String(msg.name || "");
    const reply = (m: Record<string, unknown>) => {
      if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ ...m, id: msg.id }));
    };
    const impl = name.startsWith("hedgehog_") ? primitives[name] : undefined;
    if (!impl) {
      reply({ t: "error", kind: "not_allowed", message: `${name} can't run here` });
      return;
    }
    try {
      reply({ t: "result", result: await impl(msg.args ?? {}) });
    } catch (err) {
      const e = err as PrimitiveError;
      reply({ t: "error", kind: e.kind || "browser_error", message: String(e.message || err), details: e.details });
    }
  }
}
