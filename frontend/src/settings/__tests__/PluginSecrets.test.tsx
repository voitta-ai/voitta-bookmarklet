// @vitest-environment jsdom
import React from "react";
import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import PluginSettingsPanel from "../PluginSettingsPanel";

const { saveSettings, snapshot } = vi.hoisted(() => ({
  saveSettings: vi.fn().mockResolvedValue({}),
  snapshot: { plugins: { enterprise: { mcp: { has_api_keys: { "one.example": true, "two.example": true } } } } },
}));
vi.mock("../../lib/settings", () => ({
  getSettings: () => snapshot,
  subscribeSettings: () => () => {},
  getDotted: (blob: any, key: string) => key.split(".").reduce((value, part) => value?.[part], blob),
  saveSettings,
}));

afterEach(() => { cleanup(); saveSettings.mockClear(); });

function setup() {
  return render(<PluginSettingsPanel pluginName="enterprise" backendOrigin="http://localhost"
    schema={{ fields: [{ key: "plugins.enterprise.mcp.api_keys", type: "secret_per_host", label: "API key" }] }}
    connectors={[]} hosts={["one.example", "two.example"]} refreshBusy={false} onRefreshConnectors={() => {}} />);
}

it("edits a redacted per-host token without autosaving or replacing sibling credentials", async () => {
  setup();
  const input = screen.getByLabelText("API key for one.example") as HTMLInputElement;
  expect(input.type).toBe("text");
  expect(input.classList.contains("secret")).toBe(true);
  expect(input.autocomplete).toBe("off");
  expect(input.value).toBe("");
  fireEvent.change(input, { target: { value: "new-key" } });
  expect(saveSettings).not.toHaveBeenCalled();
  fireEvent.click(screen.getAllByText("Save token")[0]);
  await waitFor(() => expect(saveSettings).toHaveBeenCalledWith("http://localhost", {
    dotted: { "plugins.enterprise.mcp.api_keys": { "one.example": "new-key" } },
  }));
});

it("clears only the selected host token", async () => {
  setup();
  fireEvent.click(screen.getAllByText("Clear token")[1]);
  await waitFor(() => expect(saveSettings).toHaveBeenCalledWith("http://localhost", {
    dotted: { "plugins.enterprise.mcp.api_keys": { "two.example": "" } },
  }));
});
