// Renders the core notices (lib/notices.ts) above the chat input.

import { useState } from "react";
import {
  clearNotice,
  sendChatMessage,
  useChatBusy,
  useNotices,
  type Notice,
  type NoticeAction,
} from "../lib/notices";

const ICON = { info: "ⓘ", warning: "⚠", error: "⛔" } as const;

export default function NoticeStack() {
  const notices = useNotices();
  if (!notices.length) return null;
  return (
    <div className="notice-stack" role="region" aria-label="Notices">
      {notices.map((n) => (
        <NoticeRow key={n.id} n={n} />
      ))}
    </div>
  );
}

function NoticeRow({ n }: { n: Notice }) {
  return (
    <div className={`notice notice-${n.level}`} role={n.level === "info" ? "status" : "alert"}>
      <div className="notice-main">
        <span className="notice-icon" aria-hidden="true">
          {ICON[n.level]}
        </span>
        <div className="notice-body">
          <div className="notice-text">{n.text}</div>
          {n.detail && <div className="notice-detail">{n.detail}</div>}
          {!!n.actions?.length && (
            <div className="notice-actions">
              {n.actions.map((a) => (
                <ActionButton key={a.label} a={a} />
              ))}
            </div>
          )}
        </div>
        {n.dismissible !== false && (
          <button
            type="button"
            className="notice-close"
            aria-label="Dismiss"
            onClick={() => clearNotice(n.id)}
          >
            ✕
          </button>
        )}
      </div>
    </div>
  );
}

function ActionButton({ a }: { a: NoticeAction }) {
  const chatBusy = useChatBusy();
  const [running, setRunning] = useState(false);
  const needsChat = a.send !== undefined || !!a.needsIdleChat;
  const disabled = running || (needsChat && chatBusy);

  async function onClick() {
    if (a.href) {
      window.open(a.href, "_blank", "noopener");
      return;
    }
    setRunning(true);
    try {
      if (a.run) await a.run();
      if (a.send !== undefined) sendChatMessage(a.send);
    } catch (err) {
      console.warn("[voitta] notice action failed", err);
    } finally {
      setRunning(false);
    }
  }

  return (
    <button
      type="button"
      className={`notice-btn${a.primary ? " primary" : ""}`}
      disabled={disabled}
      title={needsChat && chatBusy ? "Wait until the assistant has finished" : undefined}
      onClick={onClick}
    >
      {running ? "…" : a.label}
    </button>
  );
}
