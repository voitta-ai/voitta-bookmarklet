// @vitest-environment jsdom
// Widget side of sign-in / site approval (src/lib/auth.ts).

import { afterEach, describe, expect, it, vi } from "vitest";
import { checkAuth, openApprove } from "../auth";

const BACKEND = "https://bookmarklet.voitta.ai";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function fakePopup() {
  const sent: { msg: unknown; target: string }[] = [];
  const win = {
    closed: false,
    postMessage: (msg: unknown, target: string) => sent.push({ msg, target }),
  } as unknown as Window;
  vi.spyOn(window, "open").mockReturnValue(win);
  return { win, sent };
}

function fromPopup(win: Window, data: unknown, origin = BACKEND) {
  window.dispatchEvent(new MessageEvent("message", { data, origin, source: win as unknown as MessageEventSource }));
}

describe("openApprove", () => {
  it("answers the approve page's hello only to the backend origin, then resolves", async () => {
    const { win, sent } = fakePopup();
    const done = openApprove(BACKEND);
    expect(window.open).toHaveBeenCalledWith(`${BACKEND}/auth/approve`, "voitta-approve", expect.any(String));
    fromPopup(win, { t: "voitta-approve-hello" });
    expect(sent).toEqual([{ msg: { t: "voitta-approve-origin" }, target: BACKEND }]);
    fromPopup(win, { t: "voitta-site-approved" });
    await expect(done).resolves.toBeUndefined();
  });

  it("ignores look-alike messages from other origins or windows", async () => {
    const { win, sent } = fakePopup();
    const done = openApprove(BACKEND);
    fromPopup(win, { t: "voitta-approve-hello" }, "https://evil.example");
    window.dispatchEvent(new MessageEvent("message", { data: { t: "voitta-approve-hello" }, origin: BACKEND }));
    expect(sent).toEqual([]);
    // user closes it (or Google's COOP cut it off) → this page regains focus → resolves
    vi.spyOn(Date, "now").mockReturnValue(Date.now() + 1000);
    window.dispatchEvent(new Event("focus"));
    await expect(done).resolves.toBeUndefined();
  });

  it("never reads popup.closed (Google's COOP makes Chrome warn on every read)", async () => {
    let reads = 0;
    const win = {
      get closed() {
        reads++;
        return false;
      },
      postMessage: () => {},
    } as unknown as Window;
    vi.spyOn(window, "open").mockReturnValue(win);
    vi.useFakeTimers();
    const done = openApprove(BACKEND);
    vi.advanceTimersByTime(5000);
    expect(reads).toBe(0);
    fromPopup(win, { t: "voitta-site-approved" });
    await expect(done).resolves.toBeUndefined();
    vi.useRealTimers();
  });
});

describe("checkAuth", () => {
  it("fills in defaults for an old backend without the site block", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ enabled: false, authenticated: false, email: null }))));
    const s = await checkAuth(BACKEND);
    expect(s.site).toEqual({ origin: null, approved: true, refused: false });
  });

  it("passes through an unapproved site", async () => {
    const body = { enabled: true, authenticated: true, email: null, site: { origin: "https://x.com", approved: false, refused: false } };
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(body))));
    const s = await checkAuth(BACKEND);
    expect(s.site.approved).toBe(false);
    expect(s.email).toBeNull();
  });
});
