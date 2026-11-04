// @vitest-environment jsdom
import React from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import MCPSettingsPanel, { PluginMCPSettings } from "../MCPSettingsPanel";
import type { MCPServer } from "../../lib/mcp";

let server: MCPServer;
let requests: { url: string; method: string; body: Record<string, unknown> }[];

beforeEach(() => {
  server = {
    id: "enterprise--vre", name: "Research", url: "https://example.com/mcp",
    url_template: null, tool_prefix: "vre_", transport: "streamable-http",
    enabled: true, builtin: true, has_token: true,
    bindings: [{ plugin: "hedgehog", enabled: false, has_token: true }], connectors: [],
  };
  requests = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, options: RequestInit) => {
    if (options.method !== "GET") {
      requests.push({ url, method: options.method!, body: JSON.parse(String(options.body || "{}")) });
    }
    return { ok: true, json: async () => ({ servers: [server] }) };
  }));
});

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("never prefills tokens and omits an untouched default token on save", async () => {
  render(<MCPSettingsPanel backendOrigin="http://localhost" plugins={[]} onSaved={() => {}} />);
  const token = await screen.findByLabelText("Default bearer token") as HTMLInputElement;
  expect(token.type).toBe("text");
  expect(token.classList.contains("secret")).toBe(true);
  expect(token.autocomplete).toBe("off");
  expect(token.value).toBe("");
  fireEvent.change(screen.getByLabelText("Display name"), { target: { value: "Renamed" } });
  fireEvent.click(screen.getByText("Save server"));
  await waitFor(() => expect(requests).toHaveLength(1));
  expect(requests[0].body.name).toBe("Renamed");
  expect(requests[0].body).not.toHaveProperty("token");
});

it("only clears the server default after an explicit clear and save", async () => {
  render(<MCPSettingsPanel backendOrigin="http://localhost" plugins={[]} onSaved={() => {}} />);
  fireEvent.click(await screen.findByLabelText("Clear saved default token"));
  fireEvent.click(screen.getByText("Save server"));
  await waitFor(() => expect(requests).toHaveLength(1));
  expect(requests[0].body.token).toBe("");
});

it("enables a server for a plugin while removing its token override", async () => {
  render(<PluginMCPSettings backendOrigin="http://localhost" pluginName="hedgehog" onSaved={() => {}} />);
  fireEvent.click(await screen.findByLabelText("Enable for this plugin"));
  fireEvent.change(screen.getByLabelText("Authentication"), { target: { value: "default" } });
  fireEvent.click(screen.getByText("Save plugin access"));
  await waitFor(() => expect(requests).toHaveLength(1));
  expect(requests[0].url).toBe("http://localhost/api/mcp/servers/enterprise--vre/plugins/hedgehog");
  expect(requests[0].body).toEqual({ enabled: true, token: "" });
});

it("keeps an unsaved token draft when refreshing the saved connection", async () => {
  server.bindings[0].enabled = true;
  render(<MCPSettingsPanel backendOrigin="http://localhost" plugins={[]} onSaved={() => {}} />);
  const input = await screen.findByLabelText("Default bearer token") as HTMLInputElement;
  fireEvent.change(input, { target: { value: "unsaved-draft" } });
  fireEvent.click(screen.getByText("Connect / refresh"));
  await screen.findByText("Connection check complete; see status below.");
  expect(input.value).toBe("unsaved-draft");
  expect(requests[0].body).not.toHaveProperty("token");
});

it("requires confirmation before deleting a custom server", async () => {
  server.builtin = false;
  render(<MCPSettingsPanel backendOrigin="http://localhost" plugins={[]} onSaved={() => {}} />);
  fireEvent.click(await screen.findByText("Remove"));
  expect(requests).toHaveLength(0);
  fireEvent.click(screen.getByText("Confirm remove"));
  await waitFor(() => expect(requests).toHaveLength(1));
  expect(requests[0].method).toBe("DELETE");
});

it("inherits the server token by default without copying it into plugin settings", async () => {
  server.bindings[0].has_token = false;
  render(<PluginMCPSettings backendOrigin="http://localhost" pluginName="hedgehog" onSaved={() => {}} />);
  const mode = await screen.findByLabelText("Authentication") as HTMLSelectElement;
  expect(mode.value).toBe("default");
  expect(screen.queryByLabelText("Bearer token override")).toBeNull();
  fireEvent.click(screen.getByLabelText("Enable for this plugin"));
  fireEvent.click(screen.getByText("Save plugin access"));
  await waitFor(() => expect(requests).toHaveLength(1));
  expect(requests[0].body).toEqual({ enabled: true });
});

it("requires an explicit plugin override and leaves the server token untouched", async () => {
  server.bindings[0].has_token = false;
  render(<PluginMCPSettings backendOrigin="http://localhost" pluginName="hedgehog" onSaved={() => {}} />);
  fireEvent.change(await screen.findByLabelText("Authentication"), { target: { value: "override" } });
  fireEvent.click(screen.getByText("Save plugin access"));
  expect(requests).toHaveLength(0);
  fireEvent.change(screen.getByLabelText("Bearer token override"), { target: { value: "plugin-only-key" } });
  fireEvent.click(screen.getByText("Save plugin access"));
  await waitFor(() => expect(requests).toHaveLength(1));
  expect(requests[0].url).toContain("/plugins/hedgehog");
  expect(requests[0].body.token).toBe("plugin-only-key");
});
