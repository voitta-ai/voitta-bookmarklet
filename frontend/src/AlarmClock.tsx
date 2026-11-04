// Header alarm clock: arm one routine on the current chat. Off by default
// (and again after every chat switch — ChatPane's scheduler disarms on
// remount). The scheduler itself lives in ChatPane; this only arms + shows.

import { useCallback, useEffect, useRef, useState } from "react";
import {
  type LogEntry,
  type Outcome,
  type Routine,
  alarmStore,
  hhmmIn,
  loadRoutines,
  nextSlots,
  useAlarm,
} from "./lib/routines";

interface Props {
  backendOrigin: string;
  onEditRoutines: (routineId: string | null) => void; // null = new routine
}

export function ClockIcon({ size = 15 }: { size?: number }) {
  return (
    <svg viewBox="0 0 20 20" width={size} height={size} aria-hidden="true" className="clock-svg">
      {/* bells */}
      <path className="bell" d="M2.2 5.6a3.6 3.6 0 0 1 4.4-3.9z" />
      <path className="bell" d="M17.8 5.6a3.6 3.6 0 0 0-4.4-3.9z" />
      {/* face */}
      <circle className="face" cx="10" cy="11" r="6.4" />
      <path className="hands" d="M10 7.6V11l2.4 1.6" />
      {/* feet */}
      <path className="hands" d="M5.6 16.4 4.4 18M14.4 16.4l1.2 1.6" />
    </svg>
  );
}

const OUTCOME: Record<Outcome, { icon: string; text: string; cls: string }> = {
  fired: { icon: "✓", text: "fired", cls: "ok" },
  busy: { icon: "⏭", text: "missed: chat was busy", cls: "miss" },
  asking: { icon: "❔", text: "missed: waiting on your answer", cls: "miss" },
  stale: { icon: "💤", text: "missed: tab was asleep", cls: "miss" },
  elsewhere: { icon: "⧉", text: "fired in another tab", cls: "other" },
  error: { icon: "⚠", text: "failed", cls: "miss" },
};

function relIn(ms: number): string {
  const m = Math.round(ms / 60_000);
  if (m < 1) return "now";
  if (m < 60) return `in ${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `in ${h}h ${m % 60}m`;
  return `in ${Math.floor(h / 24)}d`;
}

function snippet(s: string, n = 34) {
  const one = s.replace(/\s+/g, " ").trim();
  return one.length > n ? `${one.slice(0, n - 1)}…` : one;
}

export default function AlarmClock({ backendOrigin, onEditRoutines }: Props) {
  const st = useAlarm();
  const [open, setOpen] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const rootRef = useRef<HTMLDivElement | null>(null);

  const armed = st.routines?.find((r) => r.id === st.armedId) ?? null;
  const picked: Routine | null =
    armed ?? st.routines?.find((r) => r.id === st.lastPickId) ?? st.routines?.[0] ?? null;

  // Clock face label ("next 14:30") stays fresh; ringing wiggle times out.
  useEffect(() => {
    if (!armed && !open) return;
    const id = window.setInterval(() => setNow(Date.now()), 15_000);
    return () => window.clearInterval(id);
  }, [armed, open]);
  useEffect(() => {
    if (st.ringingUntil <= Date.now()) return;
    const id = window.setTimeout(() => setNow(Date.now()), st.ringingUntil - Date.now() + 50);
    return () => window.clearTimeout(id);
  }, [st.ringingUntil]);

  const refresh = useCallback(async () => {
    try {
      await loadRoutines(backendOrigin);
      setErr(null);
    } catch (e) {
      setErr(String(e));
    }
  }, [backendOrigin]);
  useEffect(() => {
    if (st.routines === null) void refresh();
  }, [st.routines, refresh]);

  useEffect(() => {
    if (!open) return;
    setNow(Date.now());
    alarmStore.seen();
    void refresh();
    // The widget lives in a closed shadow root: a document listener only
    // sees the host element, so inside-widget clicks are judged on the
    // shadow root (real targets) and the document listener only handles
    // clicks on the host page itself.
    const shadow = rootRef.current?.getRootNode();
    const host = shadow instanceof ShadowRoot ? shadow.host : null;
    const onInner = (e: Event) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onPage = (e: MouseEvent) => {
      if (!host) onInner(e);
      else if (e.target !== host) setOpen(false);
    };
    const onBlur = () => setOpen(false);
    shadow?.addEventListener("mousedown", onInner);
    document.addEventListener("mousedown", onPage);
    window.addEventListener("blur", onBlur);
    return () => {
      shadow?.removeEventListener("mousedown", onInner);
      document.removeEventListener("mousedown", onPage);
      window.removeEventListener("blur", onBlur);
    };
  }, [open, refresh]);

  const next = armed ? nextSlots(armed, now, 1)[0] : undefined;
  const ringing = st.ringingUntil > now;
  const fired = st.log.filter((l) => l.outcome === "fired").length;
  const missed = st.log.filter((l) => OUTCOME[l.outcome].cls === "miss").length;

  const title = armed
    ? `${armed.name}${next ? ` · next ${hhmmIn(next.at, armed.tz)} (${relIn(next.at - now)})` : " · nothing scheduled"}` +
      ` · ${fired} fired, ${missed} missed`
    : "Routines — alarm is off for this chat";

  function toggle() {
    if (armed) alarmStore.disarm();
    else if (picked) alarmStore.arm(picked.id);
  }

  function choose(id: string) {
    if (id === "__new__") {
      setOpen(false);
      onEditRoutines(null);
      return;
    }
    if (armed) alarmStore.arm(id); // switching routines re-arms fresh
    else alarmStore.pick(id);
  }

  const upcoming = picked ? nextSlots(picked, now, 4) : [];
  const chimeById = (r: Routine | null, id: string) => r?.chimes.find((c) => c.id === id);

  return (
    <div className="alarm" ref={rootRef}>
      <button
        type="button"
        className={`hbtn alarm-btn${armed ? " armed" : ""}${ringing ? " ringing" : ""}`}
        title={title}
        aria-label={title}
        aria-pressed={!!armed}
        onClick={() => setOpen((v) => !v)}
      >
        <ClockIcon />
        {armed && next && <span className="alarm-next">{hhmmIn(next.at, armed.tz)}</span>}
        {st.unseenMiss && <span className="alarm-dot" aria-label="a chime was missed" />}
      </button>

      {open && (
        <div className="alarm-pop" role="dialog" aria-label="Routine alarm">
          <div className="alarm-row alarm-head">
            <ClockIcon size={16} />
            <span className="alarm-title">Alarm for this chat</span>
            <button
              type="button"
              role="switch"
              aria-checked={!!armed}
              className={`alarm-switch${armed ? " on" : ""}`}
              disabled={!picked}
              title={armed ? "Disarm" : "Arm"}
              onClick={toggle}
            >
              <span />
            </button>
          </div>

          {st.routines !== null && st.routines.length === 0 ? (
            <div className="alarm-empty">
              No routines yet.
              <button type="button" className="alarm-link" onClick={() => { setOpen(false); onEditRoutines(null); }}>
                ＋ Create your first routine
              </button>
            </div>
          ) : (
            <>
              <div className="alarm-row">
                <span className="alarm-label">Routine</span>
                <select value={picked?.id ?? ""} onChange={(e) => choose(e.currentTarget.value)}>
                  {st.routines?.map((r) => (
                    <option key={r.id} value={r.id}>{r.name}</option>
                  ))}
                  <option value="__new__">＋ New routine…</option>
                </select>
              </div>

              <div className="alarm-sec">
                Upcoming <span className="alarm-tz">({picked?.tz})</span>
              </div>
              {upcoming.length === 0 && <div className="alarm-item muted">Nothing scheduled in the next week</div>}
              {upcoming.map((s) => (
                <div className="alarm-item" key={`${s.chime.id}-${s.at}`} title={s.chime.prompt}>
                  <span className="alarm-icon">🔔</span>
                  <span className="alarm-time">{hhmmIn(s.at, picked!.tz)}</span>
                  <span className="alarm-text">{snippet(s.chime.prompt)}</span>
                  <span className="alarm-rel">{relIn(s.at - now)}</span>
                </div>
              ))}

              {armed && (
                <>
                  <div className="alarm-sec">Since armed</div>
                  {st.log.length === 0 && <div className="alarm-item muted">Nothing yet — waiting for the first chime</div>}
                  {st.log.slice(0, 8).map((l: LogEntry, i) => {
                    const o = OUTCOME[l.outcome];
                    const c = chimeById(armed, l.chimeId);
                    return (
                      <div className={`alarm-item ${o.cls}`} key={`${l.at}-${l.chimeId}-${i}`} title={c?.prompt}>
                        <span className="alarm-icon">{o.icon}</span>
                        <span className="alarm-time">{hhmmIn(l.at, armed.tz)}</span>
                        <span className="alarm-text">{o.text}</span>
                      </div>
                    );
                  })}
                </>
              )}
            </>
          )}

          {err && <div className="alarm-err">{err}</div>}

          <div className="alarm-foot">
            <button
              type="button"
              className="alarm-link"
              onClick={() => { setOpen(false); onEditRoutines(picked?.id ?? null); }}
            >
              ✎ Edit routines…
            </button>
            <span className="alarm-hint">Busy chat ⇒ chime is skipped</span>
          </div>
        </div>
      )}
    </div>
  );
}
