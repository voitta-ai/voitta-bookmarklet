// Routines: named collections of scheduled prompts ("chimes"). One routine
// can be armed on the current chat; when a chime comes due the chat receives
// its prompt as a user turn — unless the chat is busy, in which case that
// chime is dropped (never queued).
//
// This module holds (1) the pure schedule math (time-zone aware, unit
// tested), (2) a tiny external store shared by the header clock (which arms
// and shows state) and ChatPane (which owns `busy` + `sendMessage` and runs
// the scheduler), and (3) the backend calls.
//
// Arming is per tab and in memory only: a new or switched chat (ChatPane
// remount) always starts disarmed.

import { useSyncExternalStore } from "react";

// ── types (mirror backend/app/services/routines.py) ─────────────────────────

export interface Chime {
  id: string;
  mode: "every" | "at";
  every_min?: number;
  start?: string; // "HH:MM"
  end?: string;
  times?: string[];
  days: number[]; // 0 = Mon … 6 = Sun; empty = every day
  prompt: string;
}

export interface Routine {
  id: string;
  name: string;
  tz: string;
  chimes: Chime[];
}

export const MIN_EVERY_MIN = 10;

// ── time-zone math ──────────────────────────────────────────────────────────

interface ZParts {
  y: number;
  mo: number; // 1-12
  d: number;
  h: number;
  mi: number;
  wd: number; // 0 = Mon … 6 = Sun
}

const WD: Record<string, number> = { Mon: 0, Tue: 1, Wed: 2, Thu: 3, Fri: 4, Sat: 5, Sun: 6 };
const fmtCache = new Map<string, Intl.DateTimeFormat>();

function fmt(tz: string): Intl.DateTimeFormat {
  let f = fmtCache.get(tz);
  if (!f) {
    f = new Intl.DateTimeFormat("en-US", {
      timeZone: tz,
      hourCycle: "h23",
      year: "numeric",
      month: "numeric",
      day: "numeric",
      hour: "numeric",
      minute: "numeric",
      weekday: "short",
    });
    fmtCache.set(tz, f);
  }
  return f;
}

export function zonedParts(ms: number, tz: string): ZParts {
  const out: Record<string, string> = {};
  for (const p of fmt(tz).formatToParts(new Date(ms))) out[p.type] = p.value;
  return {
    y: +out.year,
    mo: +out.month,
    d: +out.day,
    h: +out.hour % 24,
    mi: +out.minute,
    wd: WD[out.weekday] ?? 0,
  };
}

/** Wall-clock time in `tz` → epoch ms. Nonexistent (DST gap) times shift forward. */
export function zonedToUtc(y: number, mo: number, d: number, h: number, mi: number, tz: string): number {
  const want = Date.UTC(y, mo - 1, d, h, mi);
  let guess = want;
  for (let i = 0; i < 2; i++) {
    const p = zonedParts(guess, tz);
    const seen = Date.UTC(p.y, p.mo - 1, p.d, p.h, p.mi);
    guess += want - seen;
  }
  return guess;
}

export function isValidTz(tz: string): boolean {
  try {
    fmt(tz);
    return true;
  } catch {
    return false;
  }
}

export function localTz(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

export function allTimeZones(): string[] {
  const fn = (Intl as unknown as { supportedValuesOf?: (k: string) => string[] }).supportedValuesOf;
  try {
    if (fn) return fn("timeZone");
  } catch {
    /* old browser */
  }
  return ["UTC", "America/New_York", "America/Chicago", "America/Los_Angeles", "Europe/London", "Europe/Berlin", "Asia/Tokyo"];
}

const toMin = (hhmm: string) => {
  const [h, m] = hhmm.split(":").map(Number);
  return h * 60 + m;
};

/** Wall-clock minutes-of-day at which a chime rings on a day it is active. */
export function chimeMinutes(c: Chime): number[] {
  if (c.mode === "at") return (c.times ?? []).map(toMin).sort((a, b) => a - b);
  const step = Math.max(MIN_EVERY_MIN, c.every_min ?? 0);
  const out: number[] = [];
  for (let m = toMin(c.start ?? "00:00"); m <= toMin(c.end ?? "23:59"); m += step) out.push(m);
  return out;
}

export interface Slot {
  chime: Chime;
  at: number; // epoch ms
}

/** All slots of `routine` with from < at <= to, ascending. */
export function slotsBetween(routine: Routine, from: number, to: number): Slot[] {
  if (to <= from) return [];
  const out: Slot[] = [];
  const a = zonedParts(from, routine.tz);
  const b = zonedParts(to, routine.tz);
  const first = Date.UTC(a.y, a.mo - 1, a.d) - 86_400_000;
  const last = Date.UTC(b.y, b.mo - 1, b.d) + 86_400_000;
  for (let day = first; day <= last; day += 86_400_000) {
    const dt = new Date(day);
    const y = dt.getUTCFullYear(), mo = dt.getUTCMonth() + 1, d = dt.getUTCDate();
    const wd = (dt.getUTCDay() + 6) % 7;
    for (const c of routine.chimes) {
      if (c.days.length && !c.days.includes(wd)) continue;
      for (const m of chimeMinutes(c)) {
        const at = zonedToUtc(y, mo, d, Math.floor(m / 60), m % 60, routine.tz);
        if (at > from && at <= to) out.push({ chime: c, at });
      }
    }
  }
  return out.sort((x, y) => x.at - y.at);
}

/** The next `n` slots strictly after `now` (looks ahead up to 8 days). */
export function nextSlots(routine: Routine, now: number, n = 1): Slot[] {
  return slotsBetween(routine, now, now + 8 * 86_400_000).slice(0, n);
}

export function hhmmIn(ms: number, tz: string): string {
  const p = zonedParts(ms, tz);
  return `${String(p.h).padStart(2, "0")}:${String(p.mi).padStart(2, "0")}`;
}

// A slot is only fired if the tick that notices it is within this window;
// older ones (laptop asleep, tab frozen) are recorded as missed.
export const STALE_MS = 90_000;

// ── what a fired chime says in the chat ─────────────────────────────────────

export const CHIME_MARK = "⏰";

export function chimeMessage(routine: Routine, slot: Slot): string {
  const time = hhmmIn(slot.at, routine.tz);
  const p = zonedParts(slot.at, routine.tz);
  const date = `${p.y}-${String(p.mo).padStart(2, "0")}-${String(p.d).padStart(2, "0")}`;
  const body = slot.chime.prompt.replace(/\{\{\s*time\s*\}\}/g, time).replace(/\{\{\s*date\s*\}\}/g, date);
  return `${CHIME_MARK} ${routine.name} · ${time}\n${body}`;
}

/** Split a user message back into its chime header + body (null if not a chime). */
export function parseChimeMessage(text: string): { label: string; body: string } | null {
  if (!text.startsWith(`${CHIME_MARK} `)) return null;
  const nl = text.indexOf("\n");
  if (nl < 0) return null;
  return { label: text.slice(CHIME_MARK.length + 1, nl), body: text.slice(nl + 1) };
}

// ── store ───────────────────────────────────────────────────────────────────

export type Outcome = "fired" | "busy" | "asking" | "stale" | "elsewhere" | "error";

export interface LogEntry {
  at: number; // slot time
  chimeId: string;
  outcome: Outcome;
}

export interface AlarmState {
  routines: Routine[] | null; // null = not loaded yet
  armedId: string | null;
  armedAt: number;
  log: LogEntry[]; // newest first, this arming only
  ringingUntil: number;
  unseenMiss: boolean;
  lastPickId: string | null; // routine preselected in the popover
}

let state: AlarmState = {
  routines: null,
  armedId: null,
  armedAt: 0,
  log: [],
  ringingUntil: 0,
  unseenMiss: false,
  lastPickId: null,
};
const listeners = new Set<() => void>();

function set(patch: Partial<AlarmState>) {
  state = { ...state, ...patch };
  listeners.forEach((l) => l());
}

export const alarmStore = {
  get: () => state,
  subscribe(l: () => void) {
    listeners.add(l);
    return () => listeners.delete(l);
  },
  armed(): Routine | null {
    return state.routines?.find((r) => r.id === state.armedId) ?? null;
  },
  arm(id: string) {
    set({ armedId: id, armedAt: Date.now(), log: [], unseenMiss: false, lastPickId: id });
  },
  disarm() {
    if (state.armedId !== null || state.log.length) set({ armedId: null, log: [], unseenMiss: false, ringingUntil: 0 });
  },
  pick(id: string | null) {
    set({ lastPickId: id });
  },
  setRoutines(routines: Routine[]) {
    // Editing the armed routine re-arms with the new schedule; deleting it disarms.
    const still = state.armedId && routines.some((r) => r.id === state.armedId);
    set({ routines, ...(still ? {} : { armedId: null, log: [] }) });
  },
  record(entry: LogEntry) {
    const miss = entry.outcome !== "fired" && entry.outcome !== "elsewhere";
    set({
      log: [entry, ...state.log].slice(0, 50),
      ...(entry.outcome === "fired" ? { ringingUntil: Date.now() + 2500 } : {}),
      ...(miss ? { unseenMiss: true } : {}),
    });
  },
  seen() {
    if (state.unseenMiss) set({ unseenMiss: false });
  },
};

export function useAlarm(): AlarmState {
  return useSyncExternalStore(alarmStore.subscribe, alarmStore.get, alarmStore.get);
}

// ── backend ─────────────────────────────────────────────────────────────────

export async function loadRoutines(origin: string): Promise<Routine[]> {
  const r = await fetch(`${origin}/api/routines`, { credentials: "include" });
  if (!r.ok) throw new Error(`status ${r.status}`);
  const body = (await r.json()) as { routines: Routine[] };
  alarmStore.setRoutines(body.routines);
  return body.routines;
}

export async function saveRoutines(origin: string, routines: Routine[]): Promise<Routine[]> {
  const r = await fetch(`${origin}/api/routines`, {
    method: "PUT",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ routines }),
  });
  if (!r.ok) {
    let detail = `status ${r.status}`;
    try {
      detail = ((await r.json()) as { detail?: string }).detail ?? detail;
    } catch {
      /* not json */
    }
    throw new Error(detail);
  }
  const body = (await r.json()) as { routines: Routine[] };
  alarmStore.setRoutines(body.routines);
  return body.routines;
}

export async function claimSlot(origin: string, chat: string, routineId: string, slot: Slot): Promise<boolean> {
  const r = await fetch(`${origin}/api/routines/claim`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat, routine_id: routineId, chime_id: slot.chime.id, slot_ms: slot.at }),
  });
  if (!r.ok) throw new Error(`claim failed: ${r.status}`);
  return ((await r.json()) as { ok: boolean }).ok;
}

// ── scheduler decision (pure; ChatPane does the I/O) ────────────────────────

export interface TickPlan {
  candidate: Slot | null; // the one slot to try firing
  missed: { slot: Slot; outcome: Outcome }[];
}

/**
 * Given the slots that came due since the last tick, choose at most one to
 * fire: the newest fresh one, and only if the chat is idle. Everything else
 * is dropped — a chime never queues behind another.
 */
export function planTick(due: Slot[], now: number, busy: boolean, asking: boolean): TickPlan {
  const missed: TickPlan["missed"] = [];
  const fresh = due.filter((s) => now - s.at <= STALE_MS);
  const newest = fresh.length ? fresh[fresh.length - 1] : null;
  for (const s of due) {
    if (s === newest) continue;
    missed.push({ slot: s, outcome: now - s.at > STALE_MS ? "stale" : "busy" });
  }
  if (!newest) return { candidate: null, missed };
  if (busy || asking) {
    missed.push({ slot: newest, outcome: asking ? "asking" : "busy" });
    return { candidate: null, missed };
  }
  return { candidate: newest, missed };
}
