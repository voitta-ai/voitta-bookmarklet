// hg.voitta.ai panel — loaded by the Hedgehog bookmarklet into app.stocktrak.com.
//
// No sign-in. The first time it runs it asks hg.voitta.ai for an API key and keeps it in this
// site's own storage (StockTrak's localStorage), then shows it with copy-paste setup for Claude
// Code, Claude Desktop and REST. While the panel is open, API calls for that key run in this tab
// (./link.ts). Also: "Allow trading" and "Keep StockTrak session alive" switches (both on by
// default), the API's recent activity, and the StockTrak logout banner from the Hedgehog plugin.

import { useCallback, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { setBackendOrigin } from "../lib/bridge";
import NoticeStack from "../chat/NoticeStack";
import notices from "../styles/components/notices.css?inline";
import { HgLink, type LinkState } from "./link";
// Registers the Hedgehog primitives and starts session handling (logout detection, keep-alive).
import "../../../plugins/hedgehog/frontend/widget";
import { refreshKeepalive } from "../../../plugins/hedgehog/frontend/session";

type W = { __voittaHgBase?: string; __voittaHgKeepalive?: boolean; __voittaHg?: { show: () => void } };
const w = window as unknown as W;
const BASE = (w.__voittaHgBase || "https://hg.voitta.ai").replace(/\/+$/, "");
const STORE = "voitta.hg.key";
const HOST_ID = "voitta-hg-panel";
const POLL_MS = 10_000;

interface Me {
  id: number;
  prefix: string;
  trading: boolean;
  keepalive: boolean;
  tab: { connected: boolean; tabs: { id: number; current: boolean; page: string; active_at: number }[] };
}
interface Act {
  at: number;
  via: string;
  op: string;
  ok: boolean;
  code: string | null;
  ms: number | null;
}

function readKey(): string | null {
  try {
    return localStorage.getItem(STORE);
  } catch {
    return null;
  }
}

function saveKey(k: string): void {
  try {
    localStorage.setItem(STORE, k);
  } catch {
    /* storage blocked: the key lives for this page only */
  }
}

async function api<T>(path: string, key: string | null, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = { ...(init.headers as Record<string, string>) };
  if (key) headers.Authorization = `Bearer ${key}`;
  if (init.body) headers["Content-Type"] = "application/json";
  const r = await fetch(`${BASE}${path}`, { ...init, headers, credentials: "omit" });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(body?.error?.message || `HTTP ${r.status}`), { status: r.status });
  return body as T;
}

async function mintKey(): Promise<string> {
  const k = (await api<{ key: string }>("/hg/keys", null, { method: "POST" })).key;
  saveKey(k);
  return k;
}

function ago(ts: number): string {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return `${Math.round(s)} s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function snippets(key: string): { id: string; label: string; text: string; note: string }[] {
  return [
    {
      id: "claude-code",
      label: "Claude Code",
      note: "Run in a terminal. Then ask Claude about your Hedgehog account.",
      text: `claude mcp add --transport http hedgehog ${BASE}/mcp/ --header "Authorization: Bearer ${key}"`,
    },
    {
      id: "claude-desktop",
      label: "Claude Desktop",
      note: "",
      text: "", // rendered as a download button (DesktopInstall), not a snippet
    },
    {
      id: "rest",
      label: "REST",
      note: `Any language. All endpoints: ${BASE}/docs`,
      text: `curl -H "Authorization: Bearer ${key}" ${BASE}/v1/account`,
    },
  ];
}

// Claude Desktop: a one-click extension (.mcpb) with this key inside. Fetched with the key in a
// header (never in a URL), then saved as a file the user double-clicks. A new download always has a
// higher version, so after Regenerate it installs over the old one and replaces the key.
function DesktopInstall({ apiKey, regenerated }: { apiKey: string; regenerated: boolean }) {
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function download() {
    setBusy(true);
    setErr(null);
    try {
      const r = await fetch(`${BASE}/hg/claude-desktop.mcpb`, {
        method: "POST",
        headers: { Authorization: `Bearer ${apiKey}` },
        credentials: "omit",
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "Hedgehog.mcpb";
      a.style.display = "none";
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 30_000);
      setDone(true);
    } catch (e) {
      setErr(`Download failed: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="desk">
      {regenerated && (
        <div className="desk-warn">Your key changed: download and install again to update Claude Desktop.</div>
      )}
      <button type="button" className="pbtn" onClick={download} disabled={busy}>
        {busy ? "Preparing…" : "⬇ Download for Claude Desktop"}
      </button>
      <ol className="steps">
        <li>Open the downloaded <b>Hedgehog.mcpb</b> (double-click it).</li>
        <li>Claude Desktop shows <b>Install</b>: click it.</li>
        <li>Ask Claude about your Hedgehog account. Keep this StockTrak tab open.</li>
      </ol>
      <div className="muted small">
        Your key is inside the file, so keep it private. Downloading again installs over the old one.
      </div>
      {done && <div className="note">Downloaded. Open it to install.</div>}
      {err && <div className="err">{err}</div>}
    </div>
  );
}

function Switch({ on, label, sub, onChange }: { on: boolean; label: string; sub: string; onChange: (v: boolean) => void }) {
  return (
    <label className="sw">
      <span className="sw-text">
        <b>{label}</b>
        <span className="muted">{sub}</span>
      </span>
      <button type="button" role="switch" aria-checked={on} className={`sw-btn${on ? " on" : ""}`} onClick={() => onChange(!on)}>
        <span />
      </button>
    </label>
  );
}

function Panel({ onHide }: { onHide: () => void }) {
  const [key, setKey] = useState<string | null>(readKey());
  const [me, setMe] = useState<Me | null>(null);
  const [acts, setActs] = useState<Act[]>([]);
  const [state, setState] = useState<LinkState>("connecting");
  const [tabId, setTabId] = useState<number | undefined>();
  const [link, setLink] = useState<HgLink | null>(null);
  const [show, setShow] = useState(false);
  const [tab, setTab] = useState("claude-code");
  const [confirmRegen, setConfirmRegen] = useState(false);
  const [regenerated, setRegenerated] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  // 1. a key: the stored one, or a new one
  useEffect(() => {
    if (key) return;
    mintKey()
      .then(setKey)
      .catch((e) => setErr(`Couldn't get an API key: ${e.message}`));
  }, [key]);

  // 2. the link, once per key
  useEffect(() => {
    if (!key) return;
    if (link) {
      link.useKey(key);
      return;
    }
    const l = new HgLink(BASE, key, {
      onState: (s, id) => {
        setState(s);
        if (id !== undefined) setTabId(id);
      },
      onBadKey: () => {
        // revoked or unknown here: start fresh with a new key
        try {
          localStorage.removeItem(STORE);
        } catch {
          /* ignore */
        }
        setKey(null);
      },
    });
    l.start();
    setLink(l);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  // 3. settings, tab list, activity
  const refresh = useCallback(async () => {
    if (!key) return;
    try {
      const [m, a] = await Promise.all([
        api<Me>("/hg/me", key),
        api<{ activity: Act[] }>("/hg/activity?limit=15", key),
      ]);
      setMe(m);
      setActs(a.activity);
      w.__voittaHgKeepalive = m.keepalive;
      refreshKeepalive();
      setErr(null);
    } catch (e) {
      if ((e as { status?: number }).status === 401) setKey(null);
      else setErr(`Couldn't reach ${BASE}: ${(e as Error).message}`);
    }
  }, [key]);

  useEffect(() => {
    refresh();
    const t = window.setInterval(() => {
      if (document.visibilityState === "visible") void refresh();
    }, POLL_MS);
    return () => clearInterval(t);
  }, [refresh, state]);

  async function setSetting(patch: Partial<Pick<Me, "trading" | "keepalive">>) {
    if (!key) return;
    try {
      const m = await api<Me>("/hg/settings", key, { method: "PATCH", body: JSON.stringify(patch) });
      setMe(m);
      w.__voittaHgKeepalive = m.keepalive;
      refreshKeepalive();
    } catch (e) {
      setErr((e as Error).message);
    }
  }

  async function regenerate() {
    if (!key) return;
    try {
      const r = await api<{ key: string }>("/hg/keys/regenerate", key, { method: "POST" });
      saveKey(r.key);
      setKey(r.key);
      setConfirmRegen(false);
      setShow(true);
      setRegenerated(true);
      setTab("claude-desktop");
      setNote("New key. The old one stopped working: update Claude Code / your scripts, and reinstall Claude Desktop.");
    } catch (e) {
      setErr((e as Error).message);
    }
  }

  async function copy(text: string, what: string) {
    try {
      await navigator.clipboard.writeText(text);
      setNote(`${what} copied.`);
    } catch {
      setNote("Couldn't copy here: select the text and copy it by hand.");
    }
  }

  const current = me?.tab.tabs.find((t) => t.current);
  const thisTabCurrent = state === "connected" && current && current.id === tabId;
  const otherTabs = (me?.tab.tabs.length ?? 0) - (state === "connected" ? 1 : 0);
  const masked = key ? `${key.slice(0, 12)}${"•".repeat(16)}` : "…";
  const snip = key ? snippets(key) : [];
  const active = snip.find((s) => s.id === tab) ?? snip[0];

  return (
    <div className="panel">
      <header>
        <span className="title">🦔 Hedgehog API</span>
        <span className={`dot ${state === "connected" ? "ok" : state === "connecting" ? "wait" : "bad"}`} />
        <span className="spacer" />
        <a className="hbtn" href={`${BASE}/docs`} target="_blank" rel="noopener">
          Docs ↗
        </a>
        <button type="button" className="hbtn" aria-label="Hide" title="Hide (click the bookmark to show)" onClick={onHide}>
          –
        </button>
      </header>
      <div className="notice-anchor">
        <NoticeStack />
      </div>
      <div className="body">
        <section className={`status ${thisTabCurrent ? "ok" : state === "connected" ? "warn" : "bad"}`}>
          {state === "connected" && thisTabCurrent && <>API calls run in this tab. Keep it open and logged in.</>}
          {state === "connected" && !thisTabCurrent && (
            <>
              API calls go to your other StockTrak tab (last used).{" "}
              <button type="button" className="link" onClick={() => link?.active(true)}>
                Use this tab
              </button>
            </>
          )}
          {state === "connecting" && <>Connecting to {BASE.replace(/^https?:\/\//, "")}…</>}
          {state === "offline" && <>Not connected. Reconnecting…</>}
          {state === "bad_key" && <>Key not recognised. Getting a new one…</>}
          {state === "connected" && otherTabs > 0 && (
            <div className="muted small">
              {otherTabs} other StockTrak tab{otherTabs === 1 ? "" : "s"} connected with this key.
            </div>
          )}
        </section>

        <section>
          <div className="label">Your API key</div>
          <div className="keybox">
            <code>{show && key ? key : masked}</code>
            <button type="button" className="sbtn" onClick={() => setShow((v) => !v)} disabled={!key}>
              {show ? "Hide" : "Show"}
            </button>
            <button type="button" className="sbtn" onClick={() => key && copy(key, "Key")} disabled={!key}>
              Copy
            </button>
          </div>
          {!confirmRegen ? (
            <button type="button" className="link small" onClick={() => setConfirmRegen(true)}>
              Regenerate key…
            </button>
          ) : (
            <div className="confirm">
              The old key stops working at once.{" "}
              <button type="button" className="sbtn danger" onClick={regenerate}>
                Regenerate
              </button>{" "}
              <button type="button" className="sbtn" onClick={() => setConfirmRegen(false)}>
                Cancel
              </button>
            </div>
          )}
        </section>

        <section>
          <Switch
            on={me?.trading ?? true}
            label="Allow trading"
            sub="Place, change and cancel orders through the API."
            onChange={(v) => setSetting({ trading: v })}
          />
          <Switch
            on={me?.keepalive ?? true}
            label="Keep StockTrak session alive"
            sub="Pings StockTrak every 4 min while this tab is open."
            onChange={(v) => setSetting({ keepalive: v })}
          />
        </section>

        <section>
          <div className="label">Connect</div>
          <div className="tabs" role="tablist">
            {snip.map((s) => (
              <button key={s.id} type="button" role="tab" aria-selected={s.id === tab} className={s.id === tab ? "on" : ""} onClick={() => setTab(s.id)}>
                {s.label}
              </button>
            ))}
          </div>
          {active && active.id === "claude-desktop" && key && <DesktopInstall apiKey={key} regenerated={regenerated} />}
          {active && active.id !== "claude-desktop" && (
            <>
              <pre className="snip">{active.text}</pre>
              <div className="row">
                <span className="muted small">{active.note}</span>
                <button type="button" className="sbtn" onClick={() => copy(active.text, active.label + " setup")}>
                  Copy
                </button>
              </div>
            </>
          )}
        </section>

        <section>
          <div className="label">Recent API activity</div>
          {acts.length === 0 ? (
            <div className="muted small">Nothing yet.</div>
          ) : (
            <div className="acts">
              {acts.map((a, i) => (
                <div key={i} className={`act${a.ok ? "" : " fail"}`}>
                  <span className="when">{ago(a.at)}</span>
                  <span className="op">{a.op}</span>
                  <span className="via">{a.via}</span>
                  <span className="res">{a.ok ? `ok${a.ms != null ? ` · ${a.ms} ms` : ""}` : a.code}</span>
                </div>
              ))}
            </div>
          )}
        </section>

        {note && <div className="note">{note}</div>}
        {err && <div className="err">{err}</div>}
      </div>
    </div>
  );
}

const CSS = `
:host{all:initial}
.root{--voitta-bg:#14171c;--voitta-surface:#1b1f26;--voitta-text:#e8eaee;--voitta-text-muted:#9aa1ad;--voitta-border:#2c323b;
 --voitta-accent:#3b82f6;--voitta-accent-fg:#fff;--voitta-warn-bg:#2b2410;--voitta-warn-border:#a07a1a;--voitta-warn-fg:#f5c451;
 --voitta-error-bg:#2b1414;--voitta-error-border:#7f1d1d;--voitta-error-fg:#f87171;--voitta-ok-fg:#4ade80;--voitta-shadow:0 8px 28px rgba(0,0,0,.35);
 font:13px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--voitta-text)}
.panel{position:fixed;top:0;right:0;height:100vh;width:min(400px,94vw);background:var(--voitta-bg);border-left:1px solid var(--voitta-border);
 box-shadow:var(--voitta-shadow);display:flex;flex-direction:column;pointer-events:auto;z-index:2147483647}
header{display:flex;align-items:center;gap:8px;padding:10px 12px;border-bottom:1px solid var(--voitta-border);flex-shrink:0}
.title{font-weight:700;font-size:14px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--voitta-text-muted)}
.dot.ok{background:var(--voitta-ok-fg)}.dot.wait{background:var(--voitta-warn-border)}.dot.bad{background:var(--voitta-error-fg)}
.spacer{flex:1}
.hbtn{background:none;border:1px solid var(--voitta-border);color:var(--voitta-text);border-radius:6px;padding:2px 8px;font:inherit;cursor:pointer;text-decoration:none}
.body{overflow-y:auto;padding:10px 12px 18px;display:flex;flex-direction:column;gap:14px}
section{display:flex;flex-direction:column;gap:6px}
.status{padding:8px 10px;border-radius:7px;border:1px solid var(--voitta-border);background:var(--voitta-surface)}
.status.ok{border-left:3px solid var(--voitta-ok-fg)}.status.warn{border-left:3px solid var(--voitta-warn-border)}.status.bad{border-left:3px solid var(--voitta-error-fg)}
.label{font-weight:600;font-size:12px;color:var(--voitta-text-muted);text-transform:uppercase;letter-spacing:.04em}
.keybox{display:flex;gap:6px;align-items:center}
.keybox code{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font:12px ui-monospace,Menlo,monospace;
 padding:6px 8px;border:1px solid var(--voitta-border);border-radius:6px;background:var(--voitta-surface);user-select:all}
.sbtn{background:var(--voitta-surface);border:1px solid var(--voitta-border);color:var(--voitta-text);border-radius:6px;padding:4px 10px;font:inherit;cursor:pointer}
.sbtn:disabled{opacity:.5;cursor:default}.sbtn.danger{border-color:var(--voitta-error-border);color:var(--voitta-error-fg)}
.link{background:none;border:none;color:var(--voitta-accent);padding:0;font:inherit;cursor:pointer;text-align:left}
.small{font-size:12px}.muted{color:var(--voitta-text-muted)}
.confirm{font-size:12px}
.sw{display:flex;align-items:center;gap:10px;justify-content:space-between;cursor:pointer}
.sw-text{display:flex;flex-direction:column}.sw-text .muted{font-size:12px}
.sw-btn{position:relative;width:36px;height:20px;flex-shrink:0;border-radius:999px;border:none;padding:0;background:#475569;cursor:pointer}
.sw-btn span{position:absolute;top:2px;left:2px;width:16px;height:16px;border-radius:50%;background:#fff;transition:transform .15s}
.sw-btn.on{background:var(--voitta-accent)}.sw-btn.on span{transform:translateX(16px)}
.tabs{display:flex;gap:4px}
.tabs button{background:none;border:1px solid var(--voitta-border);color:var(--voitta-text-muted);border-radius:6px;padding:3px 9px;font:inherit;cursor:pointer}
.tabs button.on{color:var(--voitta-text);border-color:var(--voitta-accent)}
.snip{margin:0;padding:8px 10px;border:1px solid var(--voitta-border);border-radius:6px;background:var(--voitta-surface);
 font:11.5px/1.45 ui-monospace,Menlo,monospace;white-space:pre-wrap;word-break:break-all;max-height:220px;overflow:auto;user-select:all}
.row{display:flex;gap:8px;align-items:center;justify-content:space-between}
.acts{display:flex;flex-direction:column;font-size:12px}
.act{display:grid;grid-template-columns:64px 1fr 34px auto;gap:6px;padding:2px 0;border-bottom:1px solid var(--voitta-border)}
.act .when,.act .via{color:var(--voitta-text-muted)}.act.fail .res{color:var(--voitta-error-fg)}
.note{font-size:12px;color:var(--voitta-ok-fg)}.err{font-size:12px;color:var(--voitta-error-fg)}
.desk{display:flex;flex-direction:column;gap:8px;padding:10px;border:1px solid var(--voitta-border);border-radius:8px;background:var(--voitta-surface)}
.pbtn{align-self:flex-start;background:var(--voitta-accent);border:1px solid var(--voitta-accent);color:var(--voitta-accent-fg);
 border-radius:8px;padding:8px 14px;font:inherit;font-weight:700;cursor:pointer}
.pbtn:disabled{opacity:.6;cursor:default}
.steps{margin:0;padding-left:18px;font-size:12.5px}.steps li{margin:2px 0}
.desk-warn{font-size:12px;padding:6px 8px;border-radius:6px;background:var(--voitta-warn-bg);border:1px solid var(--voitta-warn-border);color:var(--voitta-warn-fg)}
.pill{position:fixed;right:14px;bottom:14px;pointer-events:auto;z-index:2147483647;background:var(--voitta-bg);color:var(--voitta-text);
 border:1px solid var(--voitta-border);border-radius:999px;padding:6px 12px;font:600 13px system-ui,sans-serif;cursor:pointer;box-shadow:var(--voitta-shadow)}
`;

function Root() {
  const [open, setOpen] = useState(true);
  useEffect(() => {
    w.__voittaHg = { show: () => setOpen(true) };
  }, []);
  return (
    <div className="root" data-theme="dark">
      <div style={{ display: open ? "block" : "none" }}>
        <Panel onHide={() => setOpen(false)} />
      </div>
      {!open && (
        <button type="button" className="pill" onClick={() => setOpen(true)}>
          🦔 Hedgehog
        </button>
      )}
    </div>
  );
}

function mount(): void {
  if (document.getElementById(HOST_ID)) return;
  setBackendOrigin(BASE);
  const host = document.createElement("div");
  host.id = HOST_ID;
  host.style.cssText = "position:fixed;inset:0;pointer-events:none;z-index:2147483647;";
  document.documentElement.appendChild(host);
  const shadow = host.attachShadow({ mode: "closed" });
  const style = document.createElement("style");
  style.textContent = CSS + notices;
  shadow.appendChild(style);
  const mountPoint = document.createElement("div");
  shadow.appendChild(mountPoint);
  createRoot(mountPoint).render(<Root />);
}

if (!/(^|\.)stocktrak\.com$/i.test(location.hostname) && !(window as unknown as { __voittaHgAnyHost?: boolean }).__voittaHgAnyHost) {
  // Only works on StockTrak: there's nothing to connect elsewhere, and the server refuses other sites.
  alert("The Hedgehog panel only works on app.stocktrak.com.");
} else if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", mount, { once: true });
} else {
  mount();
}
