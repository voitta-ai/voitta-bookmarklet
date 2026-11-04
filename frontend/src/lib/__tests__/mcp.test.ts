import { afterEach, describe, expect, it, vi } from "vitest";
import { mcpRequest, serverPath, tokenPatch } from "../mcp";

afterEach(() => vi.unstubAllGlobals());

describe("MCP configuration requests", () => {
  it("keeps secrets unchanged for blank input and clears only explicitly", () => {
    expect(tokenPatch("", false)).toEqual({});
    expect(tokenPatch("   ", false)).toEqual({});
    expect(tokenPatch(" new-secret ", false)).toEqual({ token: "new-secret" });
    expect(tokenPatch("ignored", true)).toEqual({ token: "" });
  });

  it("sends authenticated JSON patches to encoded server paths", async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ servers: [] }) });
    vi.stubGlobal("fetch", fetch);
    await mcpRequest("http://localhost", serverPath("test/server"), "PUT", { name: "Research" });
    expect(fetch).toHaveBeenCalledWith("http://localhost/api/mcp/servers/test%2Fserver", {
      method: "PUT", credentials: "include", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: "Research" }),
    });
  });

  it("does not echo rejected request details or credentials in errors", async () => {
    const json = vi.fn().mockResolvedValue({ detail: "secret-value" });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 422, json }));
    await expect(mcpRequest("http://localhost")).rejects.toThrow("HTTP 422");
    expect(json).not.toHaveBeenCalled();
  });
});
