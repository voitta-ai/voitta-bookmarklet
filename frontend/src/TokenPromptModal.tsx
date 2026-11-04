// Inline masked-input for the Claude subscription token. Opened by the
// backend's ``prompt_claude_token`` call_fn round-trip (via lib/tokenPrompt).
//
// Rendered INLINE inside the chat pane (not a full-screen overlay): on the
// bookmarklet the host page owns document-level key listeners, so an overlay
// floating over the page can't reliably hold focus. Living in the same DOM
// subtree as the composer — which receives focus fine — fixes that, and we
// also stop key events from bubbling out to the host page.
//
// The token is returned over the call_fn ACK only — never sent as a chat
// message, so it never persists to a Chainlit step or the conversation DB.

import { useSyncExternalStore, useState, useEffect, useRef } from "react";
import {
  getPending,
  resolvePending,
  subscribePending,
  type TokenPromptResult,
} from "./lib/tokenPrompt";

export default function TokenPromptModal() {
  const pending = useSyncExternalStore(subscribePending, getPending, getPending);
  const [value, setValue] = useState("");
  const inputRef = useRef<HTMLInputElement | null>(null);

  // Clear the field whenever the prompt opens/closes so a token never lingers
  // in component state, and focus the input when it appears.
  useEffect(() => {
    setValue("");
    if (pending) {
      // Defer so the element is mounted before we focus it.
      const t = setTimeout(() => inputRef.current?.focus(), 0);
      return () => clearTimeout(t);
    }
  }, [pending]);

  if (!pending) return null;

  const finish = (result: TokenPromptResult) => {
    setValue("");
    resolvePending(result);
  };

  const submit = () => {
    const token = value.trim();
    finish(token ? { token } : { cancelled: true });
  };

  return (
    <div
      className="token-inline"
      role="group"
      aria-label="Claude subscription token"
      // Keep keystrokes inside the widget — the host page (e.g. Google Sheets)
      // registers document-level key handlers that would otherwise swallow them.
      onKeyDownCapture={(e) => e.stopPropagation()}
      onKeyUpCapture={(e) => e.stopPropagation()}
      onKeyPressCapture={(e) => e.stopPropagation()}
    >
      <div className="token-inline-title">Connect Claude subscription</div>
      <div className="token-inline-body">{pending.instructions}</div>
      <input
        ref={inputRef}
        className="token-inline-input secret"
        type="text"
        value={value}
        placeholder="sk-ant-oat01-…"
        autoComplete="off"
        autoCorrect="off"
        autoCapitalize="off"
        spellCheck={false}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") submit();
          if (e.key === "Escape") finish({ cancelled: true });
        }}
      />
      <div className="token-inline-actions">
        <button type="button" className="token-inline-cancel" onClick={() => finish({ cancelled: true })}>
          Cancel
        </button>
        <button type="button" className="token-inline-submit" onClick={submit} disabled={!value.trim()}>
          Connect
        </button>
      </div>
      <div className="token-inline-note">
        Stored locally and sent only to the Claude Code engine — never shown in chat.
      </div>
    </div>
  );
}
