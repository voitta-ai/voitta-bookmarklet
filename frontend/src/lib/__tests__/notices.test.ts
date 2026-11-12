import { afterEach, describe, expect, it, vi } from "vitest";
import {
  _resetNoticesForTests,
  clearNotice,
  clearNotices,
  getNotices,
  sendChatMessage,
  setChatHandle,
  showNotice,
} from "../notices";

afterEach(() => _resetNoticesForTests());

describe("notices", () => {
  it("stacks newest first and replaces by id in place", () => {
    showNotice({ id: "a", level: "info", text: "A" });
    showNotice({ id: "b", level: "warning", text: "B" });
    showNotice({ id: "a", level: "error", text: "A2" });
    expect(getNotices().map((n) => `${n.id}:${n.text}`)).toEqual(["b:B", "a:A2"]);
    expect(getNotices()[1].dismissible).toBe(true);
  });

  it("clears one, or all from a source", () => {
    showNotice({ id: "x", source: "hedgehog", level: "info", text: "1" });
    showNotice({ id: "y", source: "hedgehog", level: "info", text: "2" });
    showNotice({ id: "z", source: "other", level: "info", text: "3" });
    clearNotice("z");
    expect(getNotices().map((n) => n.id)).toEqual(["y", "x"]);
    clearNotices("hedgehog");
    expect(getNotices()).toEqual([]);
  });

  it("sends a chat message only when a chat is open and idle", () => {
    const send = vi.fn();
    expect(sendChatMessage("hi")).toBe(false);
    setChatHandle({ busy: true, send });
    expect(sendChatMessage("hi")).toBe(false);
    setChatHandle({ busy: false, send });
    expect(sendChatMessage("hi")).toBe(true);
    expect(send).toHaveBeenCalledWith("hi");
  });
});
