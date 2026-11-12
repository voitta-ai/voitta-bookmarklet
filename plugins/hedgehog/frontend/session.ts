// StockTrak session handling, in the user's own StockTrak tab:
//
//   • diagnostics  — page load, first OK call, user activity, keep-alive pings, every detected
//                    logout / bot check (and how), posted to the Voitta server's per-user plugin
//                    event log so they survive a logout that navigates this tab away
//   • banner       — "StockTrak session expired" with "Log in in a new tab" / "I'm back, resume"
//   • cross-tab    — StockTrak sends every tab to /logout when another tab logs out; we record it
//                    before the page goes, and explain it on the next load
//   • keep-alive   — optional read-only ping every 4 min (Settings → Plugins → Hedgehog)
//   • idle scan    — once a day, look for idle/logout timers in StockTrak's own page scripts
//
// Never stores, logs or sends a password or token: every StockTrak call is a same-origin fetch
// with the page's own cookies, and nothing ever signs in.

import { getBackendOrigin } from "../../../frontend/src/lib/bridge";
import { clearNotice, sendChatMessage, showNotice } from "../../../frontend/src/lib/notices";
import { getSettings, subscribeSettings } from "../../../frontend/src/lib/settings";
import { classifyResponse } from "./parsers";

const PLUGIN = "hedgehog";
const BANNER = "hedgehog.session";
const CROSS_TAB_BANNER = "hedgehog.crosstab";

export const KEEPALIVE_MS = 4 * 60_000;
export const HIDDEN_PAUSE_MS = 30 * 60_000;
const ACTIVITY_LOG_MS = 60_000;
const FLUSH_MS = 3_000;
const PROBE_PATH = "/account/openorders?v2=true"; // small, read-only, needs a login
const RESUME_TEXT =
  "StockTrak session is back. Continue where you left off. Before placing anything, check open " +
  "orders and history, re-preview any order, and ask me to confirm it.";

type BreakKind = "session_expired" | "site_check";
export interface SessionEvent {
  type: string;
  t?: number;
  [k: string]: unknown;
}

const st = {
  started: false,
  loadedAt: 0,
  firstOk: 0,
  lastActivity: 0,
  lastActivityLogged: 0,
  lastKeepalive: 0,
  broken: null as BreakKind | null,
  keepaliveOn: false,
  keepaliveTimer: 0,
  hiddenSince: 0,
  paused: false,
};
let queue: SessionEvent[] = [];
let flushTimer = 0;

const mins = (from: number) => (from ? Math.round(((Date.now() - from) / 60_000) * 10) / 10 : null);

// ── event log ────────────────────────────────────────────────────────────

export function logEvent(ev: SessionEvent, urgent = false): void {
  const e = { t: Date.now(), page: location.pathname, ...ev };
  queue.push(e);
  console.info("[hedgehog]", e.type, e);
  if (urgent) flush(true);
  else if (!flushTimer) flushTimer = window.setTimeout(() => flush(false), FLUSH_MS);
}

function flush(beacon: boolean): void {
  clearTimeout(flushTimer);
  flushTimer = 0;
  if (!queue.length) return;
  const batch = queue;
  queue = [];
  const url = `${getBackendOrigin()}/api/plugin-events/${PLUGIN}`;
  const body = JSON.stringify({ events: batch });
  // A beacon survives the page navigating away (the cross-tab logout does that).
  if (beacon && typeof navigator.sendBeacon === "function") {
    try {
      if (navigator.sendBeacon(url, new Blob([body], { type: "text/plain" }))) return;
    } catch {
      /* fall through */
    }
  }
  fetch(url, { method: "POST", credentials: "include", headers: { "Content-Type": "application/json" }, body, keepalive: true }).catch(
    (err) => console.warn("[hedgehog] could not record session events", err),
  );
}

async function recentEvents(limit = 50): Promise<SessionEvent[]> {
  try {
    const r = await fetch(`${getBackendOrigin()}/api/plugin-events/${PLUGIN}?limit=${limit}`, { credentials: "include" });
    if (!r.ok) return [];
    return ((await r.json()) as { events?: SessionEvent[] }).events ?? [];
  } catch {
    return [];
  }
}

// ── state changes from StockTrak calls ───────────────────────────────────

/** A StockTrak call worked: the session is alive. */
export function noteOk(): void {
  if (!st.firstOk) {
    st.firstOk = Date.now();
    logEvent({ type: "first_ok" });
  }
  if (st.broken) {
    logEvent({ type: "session_restored", after: st.broken });
    st.broken = null;
    clearNotice(BANNER);
    scheduleKeepalive();
  }
}

/** A StockTrak call (or the keep-alive) found the session gone, or the bot check in the way. */
export function noteBreak(kind: BreakKind, via: string): void {
  const first = st.broken !== kind;
  st.broken = kind;
  logEvent(
    {
      type: kind === "site_check" ? "site_check" : "logged_out",
      via,
      min_since_first_ok: mins(st.firstOk),
      min_idle: mins(st.lastActivity),
      min_since_keepalive: st.keepaliveOn ? mins(st.lastKeepalive) : null,
      keepalive: st.keepaliveOn,
    },
    true,
  );
  if (first) showBreakBanner(kind);
  stopKeepalive();
}

function showBreakBanner(kind: BreakKind, still = false): void {
  const login = kind === "session_expired";
  showNotice({
    id: BANNER,
    source: PLUGIN,
    level: "warning",
    text: login ? "StockTrak session expired. Log in again." : "StockTrak's bot check kicked in.",
    detail: still
      ? "Still not back. Finish in the other tab, then try again."
      : login
        ? "Log in in the new tab, then close it without logging out (logging out there signs this tab out too)."
        : "Open StockTrak in a new tab (that passes the check), close it, then come back here.",
    actions: [
      { label: login ? "Log in in a new tab ↗" : "Open StockTrak in a new tab ↗", href: `${location.origin}${login ? "/login" : "/dashboard"}` },
      { label: "I'm back, resume", primary: true, needsIdleChat: true, run: () => resume(kind) },
    ],
  });
}

/** Read-only check that StockTrak answers as signed in. */
export async function probe(): Promise<"ok" | BreakKind | "other"> {
  try {
    const r = await fetch(PROBE_PATH, { credentials: "same-origin", cache: "no-store", headers: { "X-Requested-With": "XMLHttpRequest" } });
    const text = await r.text();
    const v = classifyResponse({ status: r.status, url: r.url, ctype: r.headers.get("content-type") || "", text, path: PROBE_PATH });
    if (v.kind === "ok" || v.kind === "error_page") return "ok";
    if (v.kind === "session_expired" || v.kind === "site_check") return v.kind;
    return "other";
  } catch {
    return "other";
  }
}

async function resume(kind: BreakKind): Promise<void> {
  const res = await probe();
  if (res === "ok") {
    noteOk();
    sendChatMessage(RESUME_TEXT);
    return;
  }
  showBreakBanner(res === "site_check" ? "site_check" : kind, true);
}

// ── keep-alive ───────────────────────────────────────────────────────────

function onLoginPage(): boolean {
  return /^\/(login|logout)\b/i.test(location.pathname);
}

function keepaliveWanted(): boolean {
  // Never on the login page: there's no session to keep, and pings would only log more "logged out".
  if (onLoginPage()) return false;
  return Boolean((getSettings().plugins?.[PLUGIN] as Record<string, unknown> | undefined)?.keepalive);
}

function stopKeepalive(): void {
  clearTimeout(st.keepaliveTimer);
  st.keepaliveTimer = 0;
}

function scheduleKeepalive(): void {
  stopKeepalive();
  if (!st.keepaliveOn || st.broken || st.paused) return;
  const due = st.lastKeepalive ? Math.max(0, st.lastKeepalive + KEEPALIVE_MS - Date.now()) : KEEPALIVE_MS;
  st.keepaliveTimer = window.setTimeout(keepaliveTick, due);
}

async function keepaliveTick(): Promise<void> {
  st.keepaliveTimer = 0;
  if (!st.keepaliveOn || st.broken) return;
  if (document.visibilityState === "hidden" && st.hiddenSince && Date.now() - st.hiddenSince > HIDDEN_PAUSE_MS) {
    st.paused = true;
    logEvent({ type: "keepalive_paused", reason: "tab hidden 30 min", min_hidden: mins(st.hiddenSince) });
    return;
  }
  const res = await probe();
  st.lastKeepalive = Date.now();
  if (res === "ok") {
    logEvent({ type: "keepalive", ok: true });
    noteOk();
  } else if (res === "session_expired" || res === "site_check") {
    noteBreak(res, "keepalive");
    return;
  } else {
    logEvent({ type: "keepalive", ok: false });
  }
  scheduleKeepalive();
}

function applyKeepaliveSetting(): void {
  const want = keepaliveWanted();
  if (want === st.keepaliveOn) return;
  st.keepaliveOn = want;
  logEvent({ type: want ? "keepalive_on" : "keepalive_off" });
  if (want) scheduleKeepalive();
  else stopKeepalive();
}

// ── page signals ─────────────────────────────────────────────────────────

function onActivity(): void {
  st.lastActivity = Date.now();
  if (st.lastActivity - st.lastActivityLogged >= ACTIVITY_LOG_MS) {
    st.lastActivityLogged = st.lastActivity;
    logEvent({ type: "activity" });
  }
}

function onVisibility(): void {
  if (document.visibilityState === "hidden") {
    st.hiddenSince = Date.now();
    flush(true);
    return;
  }
  st.hiddenSince = 0;
  if (st.paused) {
    st.paused = false;
    logEvent({ type: "keepalive_resumed" });
    scheduleKeepalive();
  }
}

function onStorage(e: StorageEvent): void {
  // StockTrak's own listener sends this tab to /logout on this key; record it before we go.
  if (e.key !== "logout-event") return;
  logEvent(
    {
      type: "logged_out",
      via: "cross_tab",
      min_since_first_ok: mins(st.firstOk),
      min_idle: mins(st.lastActivity),
      min_since_keepalive: st.keepaliveOn ? mins(st.lastKeepalive) : null,
      keepalive: st.keepaliveOn,
    },
    true,
  );
}

// ── idle-timer scan (diagnostic, read-only) ──────────────────────────────

const TIMER_RE = /(setTimeout|setInterval)\s*\([^;]{0,200}?(logout|log-out|signout|sign-out|session|idle|expire|timeout)/gi;
const KEYWORD_RE = /(sessiontimeout|session_timeout|idletimeout|idle_timeout|logout-event|keepalive|keep_alive|\/logout\b)/gi;

export function scanForIdleTimers(sources: { where: string; text: string }[]): { where: string; snippet: string }[] {
  const out: { where: string; snippet: string }[] = [];
  for (const s of sources) {
    for (const re of [TIMER_RE, KEYWORD_RE]) {
      re.lastIndex = 0;
      let m: RegExpExecArray | null;
      while ((m = re.exec(s.text)) && out.length < 12) {
        const a = Math.max(0, m.index - 60);
        out.push({ where: s.where, snippet: s.text.slice(a, m.index + m[0].length + 60).replace(/\s+/g, " ").trim() });
      }
    }
  }
  return out;
}

async function idleScan(prior: SessionEvent[]): Promise<void> {
  const day = 86_400_000;
  if (prior.some((e) => e.type === "idle_scan" && Number(e.t) > Date.now() - day)) return;
  const sources: { where: string; text: string }[] = [];
  document.querySelectorAll("script").forEach((el, i) => {
    const src = (el as HTMLScriptElement).src;
    if (!src && el.textContent) sources.push({ where: `inline #${i}`, text: el.textContent });
  });
  for (const el of Array.from(document.querySelectorAll<HTMLScriptElement>("script[src]")).slice(0, 15)) {
    try {
      const u = new URL(el.src, location.href);
      if (u.origin !== location.origin && !/stocktrak|neutrongroup/i.test(u.hostname)) continue;
      const r = await fetch(u.href, { cache: "force-cache" });
      if (r.ok) sources.push({ where: u.pathname, text: (await r.text()).slice(0, 2_000_000) });
    } catch {
      /* cross-origin without CORS: skip */
    }
  }
  const found = scanForIdleTimers(sources);
  logEvent({ type: "idle_scan", scripts: sources.length, findings: found.length, sample: JSON.stringify(found.slice(0, 6)).slice(0, 1500) });
}

// ── start ────────────────────────────────────────────────────────────────

function onStockTrak(): boolean {
  return /(^|\.)stocktrak\.com$/i.test(location.hostname);
}

export function startSession(): void {
  if (st.started || !onStockTrak()) return;
  st.started = true;
  st.loadedAt = Date.now();
  const onLogin = onLoginPage();
  logEvent({ type: "page_load", login_page: onLogin });

  for (const ev of ["pointerdown", "keydown", "wheel"]) document.addEventListener(ev, onActivity, { capture: true, passive: true });
  document.addEventListener("visibilitychange", onVisibility);
  window.addEventListener("pagehide", () => flush(true));
  window.addEventListener("storage", onStorage);

  st.keepaliveOn = keepaliveWanted();
  if (st.keepaliveOn) logEvent({ type: "keepalive_on" });
  subscribeSettings(() => applyKeepaliveSetting());

  void recentEvents(80).then((prior) => {
    // Opened right after a logout: say what happened.
    const last = [...prior].reverse().find((e) => e.type === "logged_out" || e.type === "session_restored" || e.type === "first_ok");
    if (last?.type === "logged_out" && Number(last.t) > Date.now() - 30 * 60_000) {
      const at = new Date(Number(last.t)).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      showNotice({
        id: CROSS_TAB_BANNER,
        source: PLUGIN,
        level: "info",
        text:
          last.via === "cross_tab"
            ? `StockTrak logged this tab out at ${at} because another StockTrak tab logged out.`
            : `The StockTrak session expired at ${at}.`,
        detail: onLogin ? "Log in here, then click the bookmarklet again." : undefined,
      });
    }
    if (!onLogin) {
      window.setTimeout(() => void idleScan(prior), 5_000);
      scheduleKeepalive();
    }
  });
}

export function _sessionStateForTests() {
  return st;
}
