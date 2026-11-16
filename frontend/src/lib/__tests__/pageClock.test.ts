// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { announceAgent, holdStore, isHeld, markStopped, readClock, releaseHold } from "../pageClock";

const w = window as unknown as { voittaPageClock?: unknown };

describe("readClock", () => {
  afterEach(() => {
    delete w.voittaPageClock;
  });

  it("uses the real clock when the page has none", () => {
    const c = readClock();
    expect(c.page).toBe(false);
    expect(c.rate).toBe(1);
    expect(Math.abs(c.now - Date.now())).toBeLessThan(1000);
  });

  it("uses the page clock and its rate when published", () => {
    w.voittaPageClock = { now: () => 1793718000000, rate: () => 390 };
    expect(readClock()).toEqual({ now: 1793718000000, rate: 390, page: true });
  });

  it("falls back on a broken page clock", () => {
    w.voittaPageClock = { now: () => { throw new Error("x"); } };
    expect(readClock().page).toBe(false);
    w.voittaPageClock = { now: () => NaN };
    expect(readClock().page).toBe(false);
  });
});

describe("announceAgent", () => {
  it("dispatches voitta:agent on changes only", () => {
    const seen: boolean[] = [];
    const on = (e: Event) => seen.push((e as CustomEvent).detail.busy);
    window.addEventListener("voitta:agent", on);
    announceAgent(true);
    announceAgent(true);
    announceAgent(false);
    window.removeEventListener("voitta:agent", on);
    expect(seen).toEqual([true, false]);
  });
});

describe("agent events", () => {
  it("marks the idle event after a manual stop, once", () => {
    const seen: unknown[] = [];
    const on = (e: Event) => seen.push((e as CustomEvent).detail);
    window.addEventListener("voitta:agent", on);
    announceAgent(true);
    markStopped();
    announceAgent(false);
    announceAgent(true);
    announceAgent(false);
    markStopped(); // nothing running: reported at once
    window.removeEventListener("voitta:agent", on);
    expect(seen).toEqual([
      { busy: true }, { busy: false, stopped: true }, { busy: true }, { busy: false }, { busy: false, stopped: true },
    ]);
  });
});

describe("hold after each run", () => {
  const record = () => {
    const seen: unknown[] = [];
    const on = (e: Event) => seen.push((e as CustomEvent).detail);
    window.addEventListener("voitta:agent", on);
    return { seen, stop: () => window.removeEventListener("voitta:agent", on) };
  };

  afterEach(() => {
    holdStore.setEnabled(false);
    announceAgent(false);
  });

  it("is off by default: a finished run releases the page at once", () => {
    expect(holdStore.get().enabled).toBe(false);
    const r = record();
    announceAgent(true);
    announceAgent(false);
    r.stop();
    expect(r.seen).toEqual([{ busy: true }, { busy: false }]);
    expect(isHeld()).toBe(false);
  });

  it("keeps the page paused after a run until the user releases it", () => {
    holdStore.setEnabled(true);
    const r = record();
    announceAgent(true);
    announceAgent(false);
    announceAgent(false); // repeats (the idle debounce) change nothing
    expect(isHeld()).toBe(true);
    expect(holdStore.get().held).toBe(true);
    releaseHold();
    releaseHold(); // once only
    r.stop();
    expect(r.seen).toEqual([{ busy: true }, { busy: true, held: true }, { busy: false, released: true }]);
    expect(isHeld()).toBe(false);
  });

  it("a new turn while held starts normally and ends in a new hold", () => {
    holdStore.setEnabled(true);
    announceAgent(true);
    announceAgent(false);
    const r = record();
    announceAgent(true);
    expect(isHeld()).toBe(false);
    announceAgent(false);
    r.stop();
    expect(r.seen).toEqual([{ busy: true }, { busy: true, held: true }]);
    expect(isHeld()).toBe(true);
  });

  it("Stop and the page's own resume both drop the hold", () => {
    holdStore.setEnabled(true);
    announceAgent(true);
    announceAgent(false);
    const r = record();
    markStopped();
    expect(isHeld()).toBe(false);
    announceAgent(true);
    announceAgent(false);
    expect(isHeld()).toBe(true);
    window.dispatchEvent(new CustomEvent("voitta:release"));
    r.stop();
    expect(isHeld()).toBe(false);
    expect(r.seen).toEqual([{ busy: false, stopped: true }, { busy: true }, { busy: true, held: true }]);
  });

  it("turning the toggle off while held releases the page", () => {
    holdStore.setEnabled(true);
    announceAgent(true);
    announceAgent(false);
    const r = record();
    holdStore.setEnabled(false);
    r.stop();
    expect(isHeld()).toBe(false);
    expect(r.seen).toEqual([{ busy: false, released: true }]);
  });
});
