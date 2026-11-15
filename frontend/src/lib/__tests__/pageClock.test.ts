// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { announceAgent, readClock } from "../pageClock";

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
