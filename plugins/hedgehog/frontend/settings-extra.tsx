// Settings → Plugins → Hedgehog, below the switches: the StockTrak session log
// (recorded by session.ts into the per-user plugin event log), with Copy and Clear.
// It answers "why does StockTrak log us out?" — see the summary line per logout.

import { useCallback, useEffect, useState } from "react";
import type { CustomPanelProps } from "../../../frontend/src/settings/types";

interface Ev {
  t: number;
  type: string;
  via?: string;
  ok?: boolean;
  min_since_first_ok?: number | null;
  min_idle?: number | null;
  min_since_keepalive?: number | null;
  keepalive?: boolean;
  [k: string]: unknown;
}

const LABEL: Record<string, string> = {
  page_load: "page load",
  first_ok: "first OK call",
  activity: "activity on page",
  keepalive: "keep-alive",
  keepalive_on: "keep-alive turned on",
  keepalive_off: "keep-alive turned off",
  keepalive_paused: "keep-alive paused (tab hidden 30 min)",
  keepalive_resumed: "keep-alive resumed",
  logged_out: "LOGGED OUT",
  site_check: "BOT CHECK",
  session_restored: "session back",
  idle_scan: "idle-timer scan",
};

export function describe(e: Ev): string {
  const time = new Date(e.t).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
  let what = LABEL[e.type] ?? e.type;
  if (e.type === "keepalive") what += e.ok ? " ok" : " failed";
  if (e.type === "logged_out" || e.type === "site_check") {
    what += e.via ? `: ${String(e.via).replace(/_/g, " ")}` : "";
    const bits = [
      e.min_since_first_ok != null ? `${e.min_since_first_ok} min after first OK call` : null,
      e.min_idle != null ? `${e.min_idle} min idle` : null,
      e.min_since_keepalive != null ? `${e.min_since_keepalive} min after last keep-alive` : e.keepalive === false ? "keep-alive off" : null,
    ].filter(Boolean);
    if (bits.length) what += ` (${bits.join(" · ")})`;
  }
  if (e.type === "idle_scan") what += `: ${e.findings ?? 0} finding(s) in ${e.scripts ?? 0} script(s)`;
  return `${time}  ${what}`;
}

export default function HedgehogSessionLog({ backendOrigin }: CustomPanelProps) {
  const [events, setEvents] = useState<Ev[] | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const url = `${backendOrigin}/api/plugin-events/hedgehog`;

  const load = useCallback(async () => {
    try {
      const r = await fetch(`${url}?limit=300`, { credentials: "include" });
      setEvents(r.ok ? (((await r.json()) as { events: Ev[] }).events ?? []) : []);
    } catch {
      setEvents([]);
    }
  }, [url]);

  useEffect(() => {
    load();
  }, [load]);

  const text = (events ?? []).map(describe).join("\n");

  async function copy() {
    const json = JSON.stringify(events ?? [], null, 1);
    try {
      await navigator.clipboard.writeText(`${text}\n\n--- raw ---\n${json}`);
      setNote("Copied.");
    } catch {
      setNote("Couldn't copy here; select the log and copy it by hand.");
    }
  }

  async function clear() {
    await fetch(url, { method: "DELETE", credentials: "include" }).catch(() => undefined);
    setNote("Cleared.");
    load();
  }

  return (
    <section style={{ marginTop: 18 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, justifyContent: "space-between" }}>
        <b>StockTrak session log</b>
        <span style={{ display: "flex", gap: 6 }}>
          <button type="button" className="secondary-btn" onClick={load}>Refresh</button>
          <button type="button" className="secondary-btn" onClick={copy} disabled={!events?.length}>Copy</button>
          <button type="button" className="secondary-btn" onClick={clear} disabled={!events?.length}>Clear</button>
        </span>
      </div>
      <p className="muted" style={{ fontSize: 12, margin: "4px 0 8px" }}>
        Recorded from your StockTrak tab: page loads, first OK call, activity, keep-alive pings, and every logout with how
        it was spotted. Send the copied log to the team after a demo where StockTrak logged you out.
      </p>
      {note && <div className="status">{note}</div>}
      {events === null ? (
        <p className="muted">Loading…</p>
      ) : events.length === 0 ? (
        <p className="muted">Nothing recorded yet. Open the bookmarklet on app.stocktrak.com.</p>
      ) : (
        <pre
          style={{
            maxHeight: 280,
            overflow: "auto",
            fontSize: 11.5,
            lineHeight: 1.45,
            padding: 8,
            border: "1px solid var(--voitta-border)",
            borderRadius: 6,
            whiteSpace: "pre-wrap",
          }}
        >
          {[...(events ?? [])].reverse().map(describe).join("\n")}
        </pre>
      )}
    </section>
  );
}
