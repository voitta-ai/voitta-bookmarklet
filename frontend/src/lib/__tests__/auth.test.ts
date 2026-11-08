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
    (win as unknown as { closed: boolean }).closed = true; // user closes it → still resolves
    await expect(done).resolves.toBeUndefined();
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
