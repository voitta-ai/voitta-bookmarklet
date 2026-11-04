// Settings → Routines: create and edit routines (named collections of
// scheduled prompts). Edits are local until Save; the whole collection is
// PUT at once and validated server-side (min interval, HH:MM, time zone…).

import { useEffect, useMemo, useState } from "react";
import {
  type Chime,
  type Routine,
  MIN_EVERY_MIN,
  allTimeZones,
  hhmmIn,
  isValidTz,
  loadRoutines,
  localTz,
  nextSlots,
  saveRoutines,
  useAlarm,
} from "../lib/routines";
import { ClockIcon } from "../AlarmClock";

interface Props {
  backendOrigin: string;
  focusId?: string | null; // routine to open; null/undefined + createNew → new one
  createNew?: boolean;
}

const DAYS = ["M", "T", "W", "T", "F", "S", "S"];
const DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const TZ_LIST_ID = "voitta-tz-list";

let seq = 0;
const tmpId = (p: string) => `${p}_new${Date.now().toString(36)}${seq++}`;

function newChime(): Chime {
  return {
    id: tmpId("c"),
    mode: "every",
    every_min: 30,
    start: "09:00",
    end: "17:00",
    times: [],
    days: [0, 1, 2, 3, 4],
    prompt: "",
  };
}

function newRoutine(n: number): Routine {
  return { id: tmpId("r"), name: n ? `Routine ${n + 1}` : "My routine", tz: localTz(), chimes: [newChime()] };
}

export default function RoutinesPanel({ backendOrigin, focusId, createNew }: Props) {
  const st = useAlarm();
  const [draft, setDraft] = useState<Routine[] | null>(null);
  const [sel, setSel] = useState<string | null>(null);
  const [status, setStatus] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [saving, setSaving] = useState(false);
  const tzs = useMemo(allTimeZones, []);

  useEffect(() => {
    loadRoutines(backendOrigin)
      .then((rs) => {
        let list = rs;
        let pick = focusId && rs.some((r) => r.id === focusId) ? focusId : rs[0]?.id ?? null;
        if (createNew || rs.length === 0) {
          const r = newRoutine(rs.length);
          list = [...rs, r];
          pick = r.id;
        }
        setDraft(structuredClone(list));
        setSel(pick);
      })
      .catch((e) => setStatus({ kind: "err", text: String(e) }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [backendOrigin]);

  const dirty = useMemo(
    () => draft !== null && JSON.stringify(draft) !== JSON.stringify(st.routines ?? []),
    [draft, st.routines],
  );

  if (!draft) return <p className="muted">{status?.text ?? "Loading routines…"}</p>;

  const cur = draft.find((r) => r.id === sel) ?? null;

  const patchRoutine = (p: Partial<Routine>) =>
    setDraft((d) => d!.map((r) => (r.id === sel ? { ...r, ...p } : r)));
  const patchChime = (cid: string, p: Partial<Chime>) =>
    patchRoutine({ chimes: cur!.chimes.map((c) => (c.id === cid ? { ...c, ...p } : c)) });

  function add() {
    const r = newRoutine(draft!.length);
    setDraft([...draft!, r]);
    setSel(r.id);
  }
  function duplicate() {
    if (!cur) return;
    const r: Routine = {
      ...structuredClone(cur),
      id: tmpId("r"),
      name: `${cur.name} (copy)`,
      chimes: cur.chimes.map((c) => ({ ...structuredClone(c), id: tmpId("c") })),
    };
    setDraft([...draft!, r]);
    setSel(r.id);
  }
  function remove() {
    if (!cur || !confirm(`Delete routine "${cur.name}"?`)) return;
    const rest = draft!.filter((r) => r.id !== cur.id);
    setDraft(rest);
    setSel(rest[0]?.id ?? null);
  }

  async function save() {
    setSaving(true);
    setStatus(null);
    try {
      const selIdx = draft!.findIndex((r) => r.id === sel);
      const saved = await saveRoutines(backendOrigin, draft!);
      setDraft(structuredClone(saved));
      setSel(saved[selIdx]?.id ?? saved[0]?.id ?? null); // server assigns real ids
      setStatus({ kind: "ok", text: "Saved." });
    } catch (e) {
      setStatus({ kind: "err", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setSaving(false);
    }
  }

  const preview = cur && isValidTz(cur.tz) && cur.chimes.length ? safeNext(cur) : [];

  return (
    <div className="routines">
      <p className="muted">
        A routine is a set of <b>chimes</b> — a schedule plus a prompt. Arm a routine on a chat with the{" "}
        <span className="routines-inline-clock"><ClockIcon size={12} /></span> in the header; each chime then arrives
        as if you typed it. If the chat is busy at that moment, the chime is skipped.
      </p>

      <div className="routines-bar">
        <select value={sel ?? ""} onChange={(e) => setSel(e.currentTarget.value)} aria-label="Routine">
          {draft.map((r) => (
            <option key={r.id} value={r.id}>
              {r.name || "(unnamed)"}
            </option>
          ))}
        </select>
        <button type="button" className="secondary-btn" onClick={add} title="New routine">＋ New</button>
        <button type="button" className="secondary-btn" onClick={duplicate} disabled={!cur} title="Duplicate">⧉</button>
        <button type="button" className="secondary-btn" onClick={remove} disabled={!cur} title="Delete">🗑</button>
      </div>

      {cur && (
        <div className="routine-card">
          <label>Name</label>
          <input type="text" value={cur.name} onChange={(e) => patchRoutine({ name: e.currentTarget.value })} />

          <label>Time zone</label>
          <input
            type="text"
            list={TZ_LIST_ID}
            value={cur.tz}
            className={isValidTz(cur.tz) ? "" : "invalid"}
            onChange={(e) => patchRoutine({ tz: e.currentTarget.value })}
          />
          <datalist id={TZ_LIST_ID}>
            {tzs.map((t) => (
              <option key={t} value={t} />
            ))}
          </datalist>

          {cur.chimes.map((c, i) => (
            <ChimeEditor
              key={c.id}
              n={i + 1}
              chime={c}
              onChange={(p) => patchChime(c.id, p)}
              onDelete={() => patchRoutine({ chimes: cur.chimes.filter((x) => x.id !== c.id) })}
            />
          ))}

          <div className="routines-row">
            <button
              type="button"
              className="secondary-btn"
              onClick={() => patchRoutine({ chimes: [...cur.chimes, newChime()] })}
            >
              🔔 ＋ Add chime
            </button>
          </div>

          {preview.length > 0 && (
            <div className="routine-preview">
              <div className="routine-preview-h">Next {preview.length} chimes</div>
              {preview.map((s) => {
                const day = new Intl.DateTimeFormat("en-US", { timeZone: cur.tz, weekday: "short" }).format(s.at);
                return (
                  <div key={`${s.chime.id}-${s.at}`} className="routine-preview-row">
                    <span>🔔</span>
                    <span className="mono">
                      {day} {hhmmIn(s.at, cur.tz)}
                    </span>
                    <span className="routine-preview-p">{s.chime.prompt || "(empty prompt)"}</span>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}

      <div className="actions">
        <button type="button" className="save-btn" onClick={() => void save()} disabled={saving || !dirty}>
          {saving ? "Saving…" : "Save routines"}
        </button>
        {dirty && <span className="muted">Unsaved changes</span>}
      </div>
      {status && <div className={`status${status.kind === "err" ? " err" : ""}`}>{status.text}</div>}
    </div>
  );
}

function safeNext(r: Routine) {
  try {
    // Only well-formed chimes; the server re-validates on save.
    const ok = r.chimes.filter((c) =>
      c.mode === "at"
        ? (c.times ?? []).every((t) => /^\d\d:\d\d$/.test(t))
        : /^\d\d:\d\d$/.test(c.start ?? "") && /^\d\d:\d\d$/.test(c.end ?? "") && (c.every_min ?? 0) >= MIN_EVERY_MIN,
    );
    return nextSlots({ ...r, chimes: ok }, Date.now(), 5);
  } catch {
    return [];
  }
}

function ChimeEditor({
  n,
  chime: c,
  onChange,
  onDelete,
}: {
  n: number;
  chime: Chime;
  onChange: (p: Partial<Chime>) => void;
  onDelete: () => void;
}) {
  const tooFast = c.mode === "every" && (c.every_min ?? 0) < MIN_EVERY_MIN;
  const toggleDay = (d: number) =>
    onChange({ days: c.days.includes(d) ? c.days.filter((x) => x !== d) : [...c.days, d].sort() });

  return (
    <div className="chime">
      <div className="chime-h">
        <span>🔔 Chime {n}</span>
        <button type="button" className="chime-del" onClick={onDelete} title="Remove chime" aria-label={`Remove chime ${n}`}>
          ✕
        </button>
      </div>

      <div className="chime-modes" role="radiogroup">
        <label className="radio">
          <input type="radio" checked={c.mode === "every"} onChange={() => onChange({ mode: "every" })} />
          🔁 Every
        </label>
        <label className="radio">
          <input
            type="radio"
            checked={c.mode === "at"}
            onChange={() => onChange({ mode: "at", times: c.times?.length ? c.times : ["09:00"] })}
          />
          📍 At these times
        </label>
      </div>

      {c.mode === "every" ? (
        <div className="chime-line">
          <input
            type="number"
            min={MIN_EVERY_MIN}
            step={5}
            value={c.every_min ?? ""}
            className={tooFast ? "invalid" : ""}
            onChange={(e) => onChange({ every_min: e.currentTarget.valueAsNumber || 0 })}
          />
          <span>min, from</span>
          <input type="time" value={c.start ?? ""} onChange={(e) => onChange({ start: e.currentTarget.value })} />
          <span>to</span>
          <input type="time" value={c.end ?? ""} onChange={(e) => onChange({ end: e.currentTarget.value })} />
        </div>
      ) : (
        <div className="chime-line">
          {(c.times ?? []).map((t, i) => (
            <span className="chime-time" key={i}>
              <input
                type="time"
                value={t}
                onChange={(e) => {
                  const times = [...(c.times ?? [])];
                  times[i] = e.currentTarget.value;
                  onChange({ times });
                }}
              />
              <button
                type="button"
                className="chime-del"
                aria-label="Remove time"
                onClick={() => onChange({ times: (c.times ?? []).filter((_, j) => j !== i) })}
              >
                ✕
              </button>
            </span>
          ))}
          <button type="button" className="secondary-btn" onClick={() => onChange({ times: [...(c.times ?? []), "12:00"] })}>
            ＋
          </button>
        </div>
      )}
      {tooFast && <div className="status err">Minimum interval is {MIN_EVERY_MIN} minutes.</div>}

      <div className="chime-days" aria-label="Days">
        {DAYS.map((d, i) => (
          <button
            type="button"
            key={i}
            title={DAY_NAMES[i]}
            className={`day${c.days.length === 0 || c.days.includes(i) ? " on" : ""}`}
            onClick={() => toggleDay(i)}
          >
            {d}
          </button>
        ))}
        <span className="muted">{c.days.length === 0 ? "every day" : ""}</span>
      </div>

      <textarea
        rows={3}
        placeholder="Prompt — sent as if you typed it. {{time}} and {{date}} are filled in."
        value={c.prompt}
        onChange={(e) => onChange({ prompt: e.currentTarget.value })}
      />
    </div>
  );
}
