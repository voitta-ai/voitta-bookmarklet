import { describe, expect, it } from "vitest";
import {
  type Routine,
  chimeMessage,
  hhmmIn,
  nextSlots,
  parseChimeMessage,
  planTick,
  slotsBetween,
  zonedToUtc,
} from "../routines";

const NY = "America/New_York";

const market: Routine = {
  id: "r1",
  name: "Market",
  tz: NY,
  chimes: [
    { id: "c1", mode: "every", every_min: 30, start: "09:30", end: "16:00", days: [0, 1, 2, 3, 4], prompt: "check {{time}}" },
    { id: "c2", mode: "at", times: ["15:55"], days: [], prompt: "close" },
  ],
};

describe("zonedToUtc", () => {
  it("handles EST and EDT offsets", () => {
    expect(new Date(zonedToUtc(2026, 1, 15, 9, 30, NY)).toISOString()).toBe("2026-01-15T14:30:00.000Z");
    expect(new Date(zonedToUtc(2026, 7, 15, 9, 30, NY)).toISOString()).toBe("2026-07-15T13:30:00.000Z");
  });
});

describe("slotsBetween", () => {
  // Tue 2026-11-03, whole NY day
  const dayStart = zonedToUtc(2026, 11, 3, 0, 0, NY);
  const dayEnd = zonedToUtc(2026, 11, 3, 23, 59, NY);

  it("expands every-N windows inclusive of both ends plus at-times", () => {
    const slots = slotsBetween(market, dayStart, dayEnd);
    const times = slots.map((s) => `${s.chime.id}@${hhmmIn(s.at, NY)}`);
    expect(times.filter((t) => t.startsWith("c1"))).toHaveLength(14); // 09:30 … 16:00
    expect(times[0]).toBe("c1@09:30");
    expect(times).toContain("c1@16:00");
    expect(times).toContain("c2@15:55");
  });

  it("respects weekdays (Saturday only has the every-day chime)", () => {
    const s = zonedToUtc(2026, 11, 7, 0, 0, NY);
    const slots = slotsBetween(market, s, s + 86_399_000);
    expect(slots.map((x) => x.chime.id)).toEqual(["c2"]);
  });

  it("is exclusive of from, inclusive of to", () => {
    const at = zonedToUtc(2026, 11, 3, 10, 0, NY);
    expect(slotsBetween(market, at, at + 1000)).toHaveLength(0);
    expect(slotsBetween(market, at - 1000, at).map((s) => s.at)).toEqual([at]);
  });

  it("nextSlots skips the weekend", () => {
    const fri = zonedToUtc(2026, 11, 6, 16, 30, NY);
    const [n1, n2] = nextSlots(market, fri, 2);
    expect(hhmmIn(n1.at, NY)).toBe("15:55"); // Saturday, every-day chime
    expect(n2.chime.id).toBe("c2");
  });
});

describe("planTick", () => {
  const at = 1_000_000_000;
  const slot = (dt: number, id = "c1") => ({ chime: { ...market.chimes[0], id }, at: at + dt });

  it("fires the newest fresh slot when idle", () => {
    const p = planTick([slot(-20_000)], at, false, false);
    expect(p.candidate?.at).toBe(at - 20_000);
    expect(p.missed).toEqual([]);
  });

  it("drops a due slot when the chat is busy — never queues", () => {
    const p = planTick([slot(-5_000)], at, true, false);
    expect(p.candidate).toBeNull();
    expect(p.missed.map((m) => m.outcome)).toEqual(["busy"]);
  });

  it("flags a pending question separately", () => {
    expect(planTick([slot(0)], at, false, true).missed[0].outcome).toBe("asking");
  });

  it("marks old slots stale and keeps only one candidate", () => {
    const p = planTick([slot(-600_000), slot(-10_000, "a"), slot(-5_000, "b")], at, false, false);
    expect(p.candidate?.chime.id).toBe("b");
    expect(p.missed.map((m) => m.outcome)).toEqual(["stale", "busy"]);
  });
});

describe("chime message", () => {
  it("round-trips the header and fills {{time}}", () => {
    const at = zonedToUtc(2026, 11, 3, 14, 30, NY);
    const text = chimeMessage(market, { chime: market.chimes[0], at });
    expect(text).toBe("⏰ Market · 14:30\ncheck 14:30");
    expect(parseChimeMessage(text)).toEqual({ label: "Market · 14:30", body: "check 14:30" });
    expect(parseChimeMessage("hello")).toBeNull();
  });
});
