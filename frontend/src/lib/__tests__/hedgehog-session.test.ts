// @vitest-environment jsdom
// StockTrak session handling: response classification, idle-timer scan, log formatting.

import { describe, expect, it } from "vitest";
import { classifyResponse } from "../../../../plugins/hedgehog/frontend/parsers";
import { scanForIdleTimers } from "../../../../plugins/hedgehog/frontend/session";
import { describe as describeEvent } from "../../../../plugins/hedgehog/frontend/settings-extra";

const ST = "https://app.stocktrak.com";
const LOGIN_HTML = '<!doctype html><title>Log In | StockTrak</title><input name="Password">';
const OOPS_HTML = "<!DOCTYPE html><html><title>Oops, something went wrong</title></html>";
const INCAP_HTML = '<html><head><META NAME="robots" CONTENT="noindex"><script src="/_Incapsula_Resource?SWJIYLWA=x"></script></head></html>';
const BLOCK_HTML = "<html><body>Request unsuccessful. Incapsula incident ID: 123-456</body></html>";

const resp = (o: Partial<{ status: number; url: string; ctype: string; text: string; path: string }>) => ({
  status: 200,
  url: `${ST}/account/openorders`,
  ctype: "text/html; charset=utf-8",
  text: "<tr><td>row</td></tr>",
  path: "/account/openorders",
  ...o,
});

describe("classifyResponse", () => {
  it.each([
    ["JSON", resp({ ctype: "application/json", text: "{}" }), { kind: "ok" }],
    ["CSV", resp({ ctype: "text/csv", text: "a,b" }), { kind: "ok" }],
    ["HTML fragment", resp({}), { kind: "ok" }],
    ["redirect to /login", resp({ url: `${ST}/login?ReturnUrl=%2f` , text: LOGIN_HTML}), { kind: "session_expired", via: "redirect" }],
    ["redirect to /logout", resp({ url: `${ST}/logout` }), { kind: "session_expired", via: "redirect" }],
    ["login page in place", resp({ text: LOGIN_HTML }), { kind: "session_expired", via: "login_page" }],
    ["401", resp({ status: 401, text: "" }), { kind: "session_expired", via: "http_401" }],
    ["403", resp({ status: 403, text: "Forbidden" }), { kind: "session_expired", via: "http_403" }],
    ["bot check (challenge)", resp({ text: INCAP_HTML }), { kind: "site_check" }],
    ["bot check as 403 is NOT a logout", resp({ status: 403, text: BLOCK_HTML }), { kind: "site_check" }],
    ["Oops page is a bad request, not a logout", resp({ text: OOPS_HTML }), { kind: "error_page" }],
    ["full page where data was expected", resp({ text: "<!DOCTYPE html><html><title>Maintenance</title></html>" }),
      { kind: "unexpected_page", title: "Maintenance" }],
    ["closedpositions is a full page by design", resp({ path: "/account/closedpositions", text: "<!DOCTYPE html><html></html>" }),
      { kind: "ok" }],
  ])("%s", (_name, r, want) => {
    expect(classifyResponse(r)).toEqual(want);
  });
});

describe("scanForIdleTimers", () => {
  it("finds logout timers and the cross-tab key, ignores unrelated timers", () => {
    const found = scanForIdleTimers([
      { where: "inline #1", text: "setTimeout(function(){ window.location='/logout' }, 1200000);" },
      { where: "inline #2", text: "window.addEventListener('storage', e => { if (e.key == 'logout-event') {} })" },
      { where: "app.js", text: "setInterval(updateClock, 1000);" },
    ]);
    expect(found.map((f) => f.where)).toEqual(expect.arrayContaining(["inline #1", "inline #2"]));
    expect(found.some((f) => f.where === "app.js")).toBe(false);
  });
});

describe("session log lines", () => {
  it("summarises a logout with the numbers that tell the causes apart", () => {
    const line = describeEvent({
      t: Date.UTC(2026, 10, 12, 19, 41),
      type: "logged_out",
      via: "login_page",
      min_since_first_ok: 39,
      min_idle: 20.5,
      min_since_keepalive: null,
      keepalive: false,
    });
    expect(line).toContain("LOGGED OUT: login page (39 min after first OK call · 20.5 min idle · keep-alive off)");
  });
});
