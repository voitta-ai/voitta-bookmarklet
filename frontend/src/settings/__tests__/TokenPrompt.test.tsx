// @vitest-environment jsdom
import React from "react";
import { afterEach, expect, it } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import TokenPromptModal from "../../TokenPromptModal";
import { requestToken, resolvePending } from "../../lib/tokenPrompt";

afterEach(() => { cleanup(); resolvePending({ cancelled: true }); });

it("uses masked text rather than password semantics and submits the unchanged token", async () => {
  const result = requestToken("Paste the test subscription token.");
  render(<TokenPromptModal />);
  const input = screen.getByPlaceholderText("sk-ant-oat01-…") as HTMLInputElement;
  expect(input.type).toBe("text");
  expect(input.classList.contains("secret")).toBe(true);
  expect(input.autocomplete).toBe("off");
  fireEvent.change(input, { target: { value: "test-token-only" } });
  fireEvent.click(screen.getByText("Connect", { exact: true }));
  await expect(result).resolves.toEqual({ token: "test-token-only" });
  expect(screen.queryByPlaceholderText("sk-ant-oat01-…")).toBeNull();
  let next!: ReturnType<typeof requestToken>;
  act(() => { next = requestToken("Enter another token."); });
  expect((screen.getByPlaceholderText("sk-ant-oat01-…") as HTMLInputElement).value).toBe("");
  fireEvent.click(screen.getByText("Cancel"));
  await expect(next).resolves.toEqual({ cancelled: true });
});
