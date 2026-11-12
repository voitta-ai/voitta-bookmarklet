// Banners ("notices") above the chat input — a core API any plugin can use.
//
//   import { showNotice, clearNotice } from ".../frontend/src/lib/notices";
//   showNotice({ id: "hedgehog.session", source: "hedgehog", level: "warning",
//                text: "StockTrak session expired. Log in again.",
//                actions: [{ label: "Log in in a new tab ↗", href: "https://…/login" },
//                          { label: "I'm back, resume", run: async () => {…} }] });
//
// Non-blocking by design: never alert()/confirm(), which freeze the page and
// any automation driving it. Notices stack (newest first); showing one with
// an existing id replaces it in place. An action can open a link (new tab),
// run a callback, or send a chat message (disabled while the agent is busy).
//
// ChatPane registers the chat handle (send + busy) so plugin code, which lives
// outside React, can send a message on the user's behalf from a button.

import { useSyncExternalStore } from "react";

export type NoticeLevel = "info" | "warning" | "error";

export interface NoticeAction {
  label: string;
  href?: string; // opens in a new tab
  run?: () => void | Promise<void>;
  send?: string; // chat message to send (needs an idle chat)
  needsIdleChat?: boolean; // disable while the agent is busy (implied by `send`)
  primary?: boolean;
}

export interface Notice {
  id: string;
  source?: string; // plugin name, for clearNotices(source)
  level: NoticeLevel;
  text: string;
  detail?: string;
  actions?: NoticeAction[];
  dismissible?: boolean; // default true
  at: number;
}

export interface ChatHandle {
  busy: boolean;
  send: (text: string) => void;
}

interface State {
  notices: Notice[];
  chat: ChatHandle | null;
}

let state: State = { notices: [], chat: null };
const listeners = new Set<() => void>();

function set(next: State) {
  state = next;
  listeners.forEach((l) => l());
}

function subscribe(l: () => void) {
  listeners.add(l);
  return () => {
    listeners.delete(l);
  };
}

export function showNotice(n: Omit<Notice, "at"> & { at?: number }): void {
  const notice: Notice = { dismissible: true, ...n, at: n.at ?? Date.now() };
  const idx = state.notices.findIndex((x) => x.id === n.id);
  const notices =
    idx >= 0
      ? state.notices.map((x, i) => (i === idx ? notice : x))
      : [notice, ...state.notices];
  set({ ...state, notices });
}

export function clearNotice(id: string): void {
  if (!state.notices.some((x) => x.id === id)) return;
  set({ ...state, notices: state.notices.filter((x) => x.id !== id) });
}

export function clearNotices(source: string): void {
  if (!state.notices.some((x) => x.source === source)) return;
  set({ ...state, notices: state.notices.filter((x) => x.source !== source) });
}

export function getNotices(): Notice[] {
  return state.notices;
}

export function useNotices(): Notice[] {
  return useSyncExternalStore(subscribe, () => state.notices, () => state.notices);
}

// ── chat handle ─────────────────────────────────────────────────────────────

export function setChatHandle(chat: ChatHandle | null): void {
  const prev = state.chat;
  if (prev === chat || (prev && chat && prev.busy === chat.busy && prev.send === chat.send)) return;
  set({ ...state, chat });
}

export function useChatBusy(): boolean {
  return useSyncExternalStore(subscribe, () => !state.chat || state.chat.busy, () => true);
}

/** Send a chat message as the user. False (and nothing sent) if no chat is open or it's busy. */
export function sendChatMessage(text: string): boolean {
  const chat = state.chat;
  if (!chat || chat.busy) return false;
  chat.send(text);
  return true;
}

export function _resetNoticesForTests(): void {
  state = { notices: [], chat: null };
  listeners.forEach((l) => l());
}
