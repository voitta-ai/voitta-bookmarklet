// The clock routines run on, and the agent-activity signal a page can react to.
//
// A page may publish its own clock — a market simulator replaying a past day at
// 390×, say — as
//
//     window.voittaPageClock = { now(): number /* epoch ms */, rate(): number /* clock-ms per real ms; 0 = paused */ }
//
// Routines then come due on that clock. Without one, the real clock is used and
// nothing changes. The widget also announces agent activity on the page window:
//
//     window.dispatchEvent(new CustomEvent("voitta:agent", { detail: { busy: true | false } }))
//
// busy=true while a turn runs or a question waits for the user, so such a page
// can pause itself for the duration of the agent's response. When the user
// presses Stop, the idle event carries stopped: true (the page should stay paused).
// With "hold after each run" on, a finished turn sends { busy: true, held: true }
// instead of idle, and { busy: false, released: true } when the user releases it.
// A page that resumes on its own (its own ▶) dispatches "voitta:release" so the
// widget drops the hold. All of it is optional and generic: pages that know
// nothing about it behave exactly as before.

export interface Clock {
  now: number; // epoch ms on this clock
  rate: number; // clock ms per real ms (1 for the real clock, 0 when the page clock is paused)
  page: boolean; // true when the page supplied it
}

interface PageClockApi {
  now: () => number;
  rate?: () => number;
}

export function readClock(): Clock {
  const api = (window as unknown as { voittaPageClock?: PageClockApi }).voittaPageClock;
  if (api && typeof api.now === "function") {
    try {
      const now = Number(api.now());
      if (Number.isFinite(now) && now > 0) {
        const r = typeof api.rate === "function" ? Number(api.rate()) : 1;
        return { now, rate: Number.isFinite(r) && r >= 0 ? r : 1, page: true };
      }
    } catch {
      /* a broken page clock falls back to the real one */
    }
  }
  return { now: Date.now(), rate: 1, page: false };
}

let agentBusy: boolean | null = null; // the agent itself, not what the page was told
let stopping = false;

// ---- hold after each run --------------------------------------------------------------
// With "hold" on, the end of a turn does not release the page: it gets
// { busy: true, held: true } and stays paused until the user releases control
// (the Release button, or the page itself sending voitta:release, e.g. ▶ in its
// own clock). Off by default; a preference kept in localStorage.

const HOLD_KEY = "voitta.holdAfterRun";
let held = false;
const listeners = new Set<() => void>();
let snap = { enabled: readHoldPref(), held: false };

function readHoldPref(): boolean {
  try {
    return localStorage.getItem(HOLD_KEY) === "1";
  } catch {
    return false;
  }
}

function changed(): void {
  snap = { enabled: snap.enabled, held };
  listeners.forEach((f) => f());
}

function dispatch(detail: Record<string, boolean>): void {
  try {
    window.dispatchEvent(new CustomEvent("voitta:agent", { detail }));
  } catch {
    /* no window (tests) */
  }
}

export const holdStore = {
  get: () => snap,
  subscribe(f: () => void): () => void {
    listeners.add(f);
    return () => listeners.delete(f);
  },
  setEnabled(on: boolean): void {
    try {
      localStorage.setItem(HOLD_KEY, on ? "1" : "0");
    } catch {
      /* private mode */
    }
    snap = { enabled: on, held };
    if (!on && held) releaseHold();
    else changed();
  },
};

export function isHeld(): boolean {
  return held;
}

/** Hand control back: the page gets { busy: false, released: true } and may resume. */
export function releaseHold(): void {
  if (!held) return;
  held = false;
  changed();
  dispatch({ busy: false, released: true });
}

/** Tell the page whether the agent is working. Repeats are dropped. The idle
 *  event that follows `markStopped()` carries `stopped: true`; with hold on, the
 *  idle event becomes { busy: true, held: true } until `releaseHold()`. */
export function announceAgent(busy: boolean): void {
  if (busy === agentBusy) return;
  const wasBusy = agentBusy === true;
  agentBusy = busy;
  if (busy) {
    if (held) {
      held = false; // the user started another turn: it ends in a new hold (if still on)
      changed();
    }
    dispatch({ busy: true });
    return;
  }
  if (stopping) {
    stopping = false;
    dispatch({ busy: false, stopped: true });
    return;
  }
  // Hold only after a turn that actually ran here. The first idle report (the widget
  // loading, a chat switch, a reconnect) starts from "unknown", not from a finished run.
  if (snap.enabled && wasBusy) {
    held = true;
    changed();
    dispatch({ busy: true, held: true });
    return;
  }
  dispatch({ busy: false });
}

/** The chat is going away (unmount, chat switch): hand the page back as plain idle and
 *  forget the agent state, so the next chat starts from "unknown" — never from a finished run. */
export function resetAgent(): void {
  const was = agentBusy;
  agentBusy = null;
  stopping = false;
  if (held) {
    held = false;
    changed();
    dispatch({ busy: false, released: true });
  } else if (was === true) {
    dispatch({ busy: false });
  }
}

/** The user pressed Stop. The page gets `{ busy: false, stopped: true }` when the
 *  turn has ended (immediately if nothing was running), so a simulation that paused
 *  for the agent can stay stopped instead of resuming. Stop never leaves a hold. */
export function markStopped(): void {
  if (agentBusy === true) {
    stopping = true;
    return;
  }
  if (held) {
    held = false;
    changed();
  }
  dispatch({ busy: false, stopped: true });
}

// The page took control back itself (e.g. ▶ in its own clock): drop the hold quietly.
try {
  window.addEventListener("voitta:release", () => {
    if (!held) return;
    held = false;
    changed();
  });
} catch {
  /* no window (tests) */
}
