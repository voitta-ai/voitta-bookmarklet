// Runs the armed routine against this chat pane. Lives in ChatPane because
// that is where `busy` and `sendMessage` are; the header clock only arms and
// displays. Mount and unmount both disarm, so every new or switched chat
// starts with the alarm off.
//
// Chimes come due on the page's clock when the page publishes one (see
// pageClock.ts: e.g. a market simulator at 390×), otherwise on the real clock.

import { useEffect, useRef } from "react";
import { announceAgent, isHeld, readClock } from "./pageClock";
import { alarmStore, chimeMessage, claimSlot, planTick, slotsBetween, STALE_MS } from "./routines";

const TICK_MS = 10_000;
// A page clock may run hundreds of times faster than real time: look often.
const PAGE_TICK_MS = 1_000;
// sendMessage → task_start takes a moment; treat the chat as busy meanwhile
// so a second chime in the same breath can't slip in.
const SEND_GRACE_MS = 8_000;

interface Opts {
  backendOrigin: string;
  chatKey: string | null; // stable across tabs for the same chat
  busy: boolean;
  asking: boolean;
  send: (text: string) => void;
}

export function useRoutineScheduler(opts: Opts) {
  const latest = useRef(opts);
  latest.current = opts;
  const lastTick = useRef(0); // clock ms
  const start = useRef(0); // clock ms when the current arming began
  const armedSeen = useRef(0); // alarmStore armedAt (real ms) that `start` belongs to
  const sentAt = useRef(0); // real ms
  const running = useRef(false);
  const tabKey = useRef(`tab:${Math.random().toString(36).slice(2, 10)}`);

  useEffect(() => {
    alarmStore.disarm();
    let timer = 0;
    let stopped = false;

    const tick = async (): Promise<boolean> => {
      const clock = readClock();
      const routine = alarmStore.armed();
      if (!routine) {
        lastTick.current = 0;
        armedSeen.current = 0;
        return clock.page;
      }
      if (running.current) return clock.page;
      running.current = true;
      try {
        const now = clock.now;
        const { armedAt } = alarmStore.get();
        if (armedSeen.current !== armedAt) {
          // A new arming starts now on this clock: never fire slots from before it.
          armedSeen.current = armedAt;
          start.current = now;
          lastTick.current = now;
        }
        if (now < lastTick.current) {
          // The page clock went back (a simulator reset): carry on from there.
          start.current = now;
          lastTick.current = now;
        }
        const from = Math.max(lastTick.current, start.current);
        lastTick.current = now;
        const due = slotsBetween(routine, from, now);
        if (!due.length) return clock.page;

        const o = latest.current;
        // A hold (the user has not released the last run yet) counts as busy: no chime on top of it.
        const busy = o.busy || isHeld() || Date.now() - sentAt.current < SEND_GRACE_MS;
        const plan = planTick(due, now, busy, o.asking, STALE_MS * Math.max(1, clock.rate));
        for (const m of plan.missed) alarmStore.record({ at: m.slot.at, chimeId: m.slot.chime.id, outcome: m.outcome });
        const slot = plan.candidate;
        if (!slot) return clock.page;

        // On a page clock the same slot time can come round again (a replayed day), so
        // duplicate-guard per arming rather than per chat.
        const chat = o.chatKey ?? tabKey.current;
        let won = true;
        try {
          won = await claimSlot(o.backendOrigin, clock.page ? `${chat}#${armedAt}` : chat, routine.id, slot);
        } catch (err) {
          // Claim is only a duplicate guard; if it fails, fire anyway.
          console.warn("[voitta] routine claim failed", err);
        }
        if (!won) {
          alarmStore.record({ at: slot.at, chimeId: slot.chime.id, outcome: "elsewhere" });
          return clock.page;
        }
        // Re-check after the await: the user may have started a turn meanwhile.
        const after = latest.current;
        if (alarmStore.armed()?.id !== routine.id) return clock.page;
        if (after.busy || after.asking) {
          alarmStore.record({ at: slot.at, chimeId: slot.chime.id, outcome: after.asking ? "asking" : "busy" });
          return clock.page;
        }
        sentAt.current = Date.now();
        // Tell the page at once (a 390× clock moves minutes in the time a turn takes to start);
        // ChatPane keeps announcing from then on, and clears this if the turn never starts.
        announceAgent(true);
        after.send(chimeMessage(routine, slot));
        alarmStore.record({ at: slot.at, chimeId: slot.chime.id, outcome: "fired" });
        window.setTimeout(() => {
          const l = latest.current;
          if (!l.busy && !l.asking) announceAgent(false);
        }, SEND_GRACE_MS);
        return clock.page;
      } finally {
        running.current = false;
      }
    };

    const loop = async () => {
      let page = false;
      try {
        page = await tick();
      } catch (err) {
        console.warn("[voitta] routine tick failed", err);
      }
      if (!stopped) timer = window.setTimeout(() => void loop(), page ? PAGE_TICK_MS : TICK_MS);
    };
    timer = window.setTimeout(() => void loop(), PAGE_TICK_MS);

    // Wake-ups from sleep/background throttling: check promptly.
    const onVis = () => {
      if (document.visibilityState === "visible") void tick();
    };
    document.addEventListener("visibilitychange", onVis);
    return () => {
      stopped = true;
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVis);
      alarmStore.disarm();
    };
  }, []);
}
