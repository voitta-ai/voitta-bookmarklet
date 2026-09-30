// Chat surface: socket connect, message list, composer with image
// attachments. On send, each attachment is uploaded to Chainlit's
// file endpoint and then referenced from the user message — the BE
// pulls them off ``cl.Message.elements`` and attaches them to the
// model call.

import {
  type IStep,
  messagesState,
  useChatData,
  useChatInteract,
  useChatMessages,
} from "@chainlit/react-client";
import { useEffect, useMemo, useState } from "react";
import { useSetRecoilState } from "recoil";
import MessageList from "./chat/MessageList";
import Composer from "./chat/Composer";
import QuestionCard from "./chat/QuestionCard";
import TokenPromptModal from "./TokenPromptModal";
import type { ImageAttachment } from "./lib/image-attach";
import { encodeFiles } from "./lib/attachments";
import { useAuthConnect } from "./lib/useAuthConnect";

// One row of /api/agent_sdk/sessions/{id}/messages (see
// backend/app/services/agent_sdk/sessions.py:_transcript_rows).
type TranscriptRow =
  | { role: "user" | "assistant"; text: string }
  | {
      role: "tool";
      name: string;
      input: string | null;
      output: string | null;
      is_error: boolean;
    };

interface Props {
  backendOrigin: string;
  hasApiKey: boolean;
  threadId?: string | null;
  // Brain (subscription) mode: replay this Agent SDK session's transcript into
  // the pane on mount. The turn is already armed to resume it server-side; this
  // just makes the prior messages visible. null = fresh conversation.
  sdkResumeSessionId?: string | null;
}

export default function ChatPane({
  backendOrigin,
  hasApiKey,
  threadId,
  sdkResumeSessionId,
}: Props) {
  // useAuthConnect is Chainlit's useChatSession, which takes no args — the
  // connection target is configured on the ChainlitContext provider, not here.
  // (backendOrigin is still used below for socket transport selection.)
  const { connect, disconnect, session } = useAuthConnect();
  const { messages } = useChatMessages();
  const { loading, elements, askUser } = useChatData();
  const { sendMessage, stopTask, uploadFile, windowMessage } = useChatInteract();
  const setMessages = useSetRecoilState(messagesState);
  const [attachments, setAttachments] = useState<ImageAttachment[]>([]);

  // Drawer sets threadIdToResumeState (and clears messages) BEFORE changing the key that
  // remounts this component. So by the time this component mounts, the Recoil atom already
  // holds the correct value and the `connect` closure captures it.
  //
  // Effect 1 (mount): just disconnect on cleanup.
  // Effect 2 (connect changes): always call connect(). Fires on mount (initial value) and
  // whenever the connect closure rebuilds (which won't happen here since the atom was set
  // before mount and doesn't change again).
  useEffect(() => {
    return () => { disconnect(); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    // On the hardened-site bridge, all backend traffic is tunnelled through
    // the popup. Force socket.io to the WebSocket transport only — its
    // XHR-polling fallback isn't covered by the WebSocket shim (and would hit
    // the page CSP's connect-src wall). On ordinary pages, leave the default.
    const bridge = (window as unknown as { __voittaBridge?: boolean }).__voittaBridge;
    connect(bridge ? { userEnv: {}, transports: ["websocket"] } : { userEnv: {} });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connect]);

  // Brain (subscription) mode: replay the resumed SDK session's transcript.
  // The Agent SDK keeps its sessions in its own store (not Chainlit's thread
  // DB), so Chainlit's resume can't render them — we fetch the text and tool
  // rows and seed the message atom ourselves (tool rows become the same "tool"
  // steps a live turn renders). Gated on the socket being up so a
  // connect-time reset can't wipe what we inject; runs once per mount (the pane
  // is re-keyed per session switch).
  useEffect(() => {
    if (!sdkResumeSessionId) return;
    if (!session?.socket) return;
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(
          `${backendOrigin}/api/agent_sdk/sessions/${encodeURIComponent(sdkResumeSessionId)}/messages`,
          { credentials: "include" },
        );
        const body = (await res.json()) as { messages?: TranscriptRow[] };
        if (cancelled) return;
        const rows = body.messages ?? [];
        // Synthetic, strictly-increasing timestamps preserve order under
        // MessageList's createdAt sort without colliding with live messages.
        const base = Date.now() - rows.length;
        const steps: IStep[] = rows.map((m, i) => {
          const common = {
            id: `sdk-${sdkResumeSessionId}-${i}`,
            createdAt: new Date(base + i).toISOString(),
          };
          if (m.role === "tool") {
            return {
              ...common,
              name: m.name,
              type: "tool",
              input: m.input ?? "",
              output: m.output ?? "",
              isError: m.is_error,
            };
          }
          return {
            ...common,
            name: m.role,
            type: m.role === "user" ? "user_message" : "assistant_message",
            output: m.text,
          };
        });
        setMessages(steps);
      } catch (err) {
        console.warn("[ChatPane] sdk transcript load failed", err);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [sdkResumeSessionId, session, backendOrigin, setMessages]);

  // Tell the backend which host/page/title the bookmarklet sits on — the
  // active-sessions window ("tasks voitta") renders these per row. Title
  // can change after load (SPAs), so observe <title> and re-post.
  useEffect(() => {
    const socket = session?.socket;
    if (!socket) return;
    const post = () => {
      windowMessage(`host:${location.host}`);
      windowMessage(`url:${location.href}`);
      windowMessage(`title:${document.title}`);
    };
    post();
    socket.on("connect", post);

    const titleEl = document.querySelector("title");
    let obs: MutationObserver | null = null;
    if (titleEl) {
      obs = new MutationObserver(() => {
        windowMessage(`title:${document.title}`);
        windowMessage(`url:${location.href}`);
      });
      obs.observe(titleEl, { childList: true });
    }
    return () => {
      socket.off("connect", post);
      obs?.disconnect();
    };
  }, [session, windowMessage]);

  // Focus beacon: mark this session as the active one whenever the host
  // tab gains focus (last-focused-wins on the backend — no blur message).
  // The voice assistant routes utterances to the last-focused session.
  useEffect(() => {
    const socket = session?.socket;
    if (!socket) return;
    const beacon = () => {
      if (document.visibilityState === "visible" && document.hasFocus()) {
        windowMessage("focus:1");
      }
    };
    window.addEventListener("focus", beacon);
    document.addEventListener("visibilitychange", beacon);
    socket.on("connect", beacon);
    beacon();
    return () => {
      window.removeEventListener("focus", beacon);
      document.removeEventListener("visibilitychange", beacon);
      socket.off("connect", beacon);
    };
  }, [session, windowMessage]);

  // ``loading`` is true between ``task_start`` and ``task_end`` socket
  // events — covers the whole turn, including the gap between tool
  // dispatches when no text is streaming. ``streaming`` on the last
  // message only catches the text-output phase, so it leaves us with
  // a greyed-out send button during tool execution. Combine both for
  // safety.
  const busy = loading || messages.some((m) => m.streaming);

  // AskUserQuestion (Claude brain): the backend sends an element-type ask
  // whose CustomElement carries the questions JSON in its props. While it is
  // pending, Chainlit has already fired task_end (busy=false), so the composer
  // stays usable — typing routes through the ask callback as a freeform
  // `response` instead of starting a (concurrent!) new turn via sendMessage.
  const askQuestions = useMemo(() => {
    if (askUser?.spec.type !== "element") return null;
    const el = elements.find((e) => e.id === askUser.spec.element_id);
    if (!el || el.type !== "custom" || el.name !== "AskUserQuestion") return null;
    const props = (el as { props?: Record<string, unknown> }).props;
    const qs = props?.questions;
    return Array.isArray(qs) && qs.length ? (qs as never[]) : null;
  }, [askUser, elements]);

  async function onAttach(files: File[]) {
    const encoded = await encodeFiles(files);
    if (encoded.length) setAttachments((prev) => [...prev, ...encoded]);
  }

  function onRemoveAttachment(i: number) {
    setAttachments((prev) => prev.filter((_, idx) => idx !== i));
  }

  async function onSend(text: string) {
    // A question is pending: the typed text answers it (freeform path). Never
    // fall through to sendMessage here — on_message has no busy-guard, so a
    // regular message would start a second brain turn under the active one.
    if (askQuestions && askUser) {
      askUser.callback({ response: text } as never);
      return;
    }
    const pending = attachments;
    setAttachments([]);

    // Upload attachments first so Chainlit has File ids before the
    // user message lands. Each upload returns ``{id}``; we batch into
    // a fileReferences list and pass to ``sendMessage``.
    const refs: { id: string }[] = [];
    for (const att of pending) {
      try {
        const file = await imageAttachmentToFile(att);
        const { promise } = uploadFile(file, () => undefined);
        const { id } = await promise;
        refs.push({ id });
      } catch (err) {
        console.warn("[voitta] upload failed", err);
      }
    }

    sendMessage(
      { output: text, name: "user", type: "user_message" },
      refs,
    );
  }

  return (
    <div className="chat-pane">
      <MessageList
        steps={messages}
        elements={elements}
        backendOrigin={backendOrigin}
        emptyHint={
          hasApiKey
            ? "Say hello to get started."
            : "Open ⚙ Settings and add an API key."
        }
      />
      <TokenPromptModal />
      {askQuestions && askUser && (
        <QuestionCard
          key={askUser.spec.step_id}
          ask={askUser}
          questions={askQuestions}
        />
      )}
      <Composer
        busy={busy}
        attachments={attachments}
        onAttach={onAttach}
        onRemoveAttachment={onRemoveAttachment}
        onSend={onSend}
        onStop={() => stopTask()}
      />
    </div>
  );
}

// Round-trip an encoded attachment back to a `File` so we can hand it
// to Chainlit's `uploadFile`. We already paid the encode cost in
// `encodeFiles`; the base64 → Blob conversion is cheap.
async function imageAttachmentToFile(att: ImageAttachment): Promise<File> {
  const bin = atob(att.data);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  const ext = att.mime.split("/")[1] || "bin";
  return new File([bytes], `paste-${Date.now()}.${ext}`, { type: att.mime });
}
