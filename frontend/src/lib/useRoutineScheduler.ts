// Runs the armed routine against this chat pane. Lives in ChatPane because
// that is where `busy` and `sendMessage` are; the header clock only arms and
// displays. Mount and unmount both disarm, so every new or switched chat
// starts with the alarm off.

import { useEffect, useRef } from "react";
import { alarmStore, chimeMessage, claimSlot, planTick, slotsBetween } from "./routines";

const TICK_MS = 10_000;
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
  const lastTick = useRef(0);
  const sentAt = useRef(0);
  const running = useRef(false);
  const tabKey = useRef(`tab:${Math.random().toString(36).slice(2, 10)}`);

  useEffect(() => {
    alarmStore.disarm();

    const tick = async () => {
      const routine = alarmStore.armed();
      if (!routine) {
        lastTick.current = 0;
        return;
      }
      if (running.current) return;
      running.current = true;
      try {
        const now = Date.now();
        const { armedAt } = alarmStore.get();
        // Never fire slots from before arming.
        const from = Math.max(lastTick.current, armedAt);
        lastTick.current = now;
        const due = slotsBetween(routine, from, now);
        if (!due.length) return;

        const o = latest.current;
        const busy = o.busy || now - sentAt.current < SEND_GRACE_MS;
        const plan = planTick(due, now, busy, o.asking);
        for (const m of plan.missed) alarmStore.record({ at: m.slot.at, chimeId: m.slot.chime.id, outcome: m.outcome });
        const slot = plan.candidate;
        if (!slot) return;

        let won = true;
        try {
          won = await claimSlot(o.backendOrigin, o.chatKey ?? tabKey.current, routine.id, slot);
        } catch (err) {
          // Claim is only a duplicate guard; if it fails, fire anyway.
          console.warn("[voitta] routine claim failed", err);
        }
        if (!won) {
          alarmStore.record({ at: slot.at, chimeId: slot.chime.id, outcome: "elsewhere" });
          return;
        }
        // Re-check after the await: the user may have started a turn meanwhile.
        const after = latest.current;
        if (alarmStore.armed()?.id !== routine.id) return;
        if (after.busy || after.asking) {
          alarmStore.record({ at: slot.at, chimeId: slot.chime.id, outcome: after.asking ? "asking" : "busy" });
          return;
        }
        sentAt.current = Date.now();
        after.send(chimeMessage(routine, slot));
        alarmStore.record({ at: slot.at, chimeId: slot.chime.id, outcome: "fired" });
      } finally {
        running.current = false;
      }
    };

    const id = window.setInterval(() => void tick(), TICK_MS);
    // Wake-ups from sleep/background throttling: check promptly.
    const onVis = () => {
      if (document.visibilityState === "visible") void tick();
    };
    document.addEventListener("visibilitychange", onVis);
    return () => {
      window.clearInterval(id);
      document.removeEventListener("visibilitychange", onVis);
      alarmStore.disarm();
    };
  }, []);
}
