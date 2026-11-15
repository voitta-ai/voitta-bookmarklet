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
// can pause itself for the duration of the agent's response. Both are optional
// and generic: pages that know nothing about them behave exactly as before.

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

let lastBusy: boolean | null = null;

/** Tell the page whether the agent is working. Repeats are dropped. */
export function announceAgent(busy: boolean): void {
  if (busy === lastBusy) return;
  lastBusy = busy;
  try {
    window.dispatchEvent(new CustomEvent("voitta:agent", { detail: { busy } }));
  } catch {
    /* no window (tests) */
  }
}
