// @vitest-environment jsdom
// The Hedgehog session-log panel (plugins/hedgehog/frontend/settings-extra.tsx) renders the log.

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";
import HedgehogSessionLog from "../../../../plugins/hedgehog/frontend/settings-extra";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

afterEach(() => {
  vi.unstubAllGlobals();
  document.body.innerHTML = "";
});

it("loads the log from the backend and shows newest first", async () => {
  const events = [
    { t: Date.UTC(2026, 10, 12, 19, 2), type: "first_ok" },
    { t: Date.UTC(2026, 10, 12, 19, 41), type: "logged_out", via: "cross_tab", min_since_first_ok: 39, min_idle: 3, keepalive: false },
  ];
  const fetchMock = vi.fn(async () => new Response(JSON.stringify({ events })));
  vi.stubGlobal("fetch", fetchMock);
  const host = document.createElement("div");
  document.body.appendChild(host);
  await act(async () => {
    createRoot(host).render(<HedgehogSessionLog pluginName="hedgehog" backendOrigin="https://bookmarklet.voitta.ai" />);
  });
  expect(fetchMock).toHaveBeenCalledWith("https://bookmarklet.voitta.ai/api/plugin-events/hedgehog?limit=300", { credentials: "include" });
  const log = host.querySelector("pre")!.textContent!;
  expect(log.indexOf("LOGGED OUT: cross tab")).toBeLessThan(log.indexOf("first OK call"));
  expect(log).toContain("39 min after first OK call · 3 min idle · keep-alive off");
});
