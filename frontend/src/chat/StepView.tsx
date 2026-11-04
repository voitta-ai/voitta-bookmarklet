// One row in the chat: user/assistant message bubble or tool step.
// Class names match styles/components/messages.css.

import { useEffect, useState } from "react";
import { type IMessageElement, type IStep } from "@chainlit/react-client";
import Markdown from "./Markdown";
import { parseChimeMessage } from "../lib/routines";
import BridgeImg from "./BridgeImg";

// ---- image zoom ----------------------------------------------------------
// Chat images render as thumbnails (attachments) or inline figures; every
// one opens a full-size lightbox on click. Esc or any click dismisses.

function Lightbox({
  url, backendOrigin, alt, onClose,
}: { url: string | undefined; backendOrigin: string; alt?: string; onClose: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="img-lightbox" role="dialog" aria-label={alt || "image"} onClick={onClose}>
      <BridgeImg url={url} backendOrigin={backendOrigin} alt={alt} />
    </div>
  );
}

function ZoomableImg({
  url, backendOrigin, alt, className,
}: { url: string | undefined; backendOrigin: string; alt?: string; className?: string }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <BridgeImg
        className={className}
        url={url}
        backendOrigin={backendOrigin}
        alt={alt}
        title="Click to view full size"
        onClick={() => setOpen(true)}
      />
      {open && (
        <Lightbox url={url} backendOrigin={backendOrigin} alt={alt} onClose={() => setOpen(false)} />
      )}
    </>
  );
}

interface Props {
  step: IStep;
  elements: IMessageElement[];
  backendOrigin: string;
}

export default function StepView({ step, elements, backendOrigin }: Props) {
  if (step.type === "user_message") {
    const images = elements.filter((e) => e.type === "image");
    return (
      <div className="msg user">
        {images.length > 0 && (
          <div className="msg-attachments">
            {images.map((el) => (
              <ZoomableImg
                key={el.id}
                className="msg-attachment"
                url={el.url}
                backendOrigin={backendOrigin}
                alt={el.name || ""}
              />
            ))}
          </div>
        )}
        <UserText text={step.output ?? ""} />
      </div>
    );
  }
  if (step.type === "assistant_message") {
    const inline = elements.filter((e) => !("display" in e) || e.display === "inline");
    return (
      <div className={`turn assistant${step.streaming ? " streaming" : ""}`}>
        <div className="msg assistant">
          {step.output ? <Markdown text={step.output} /> : null}
          {inline.map((el) => (
            <ElementView key={el.id} element={el} backendOrigin={backendOrigin} />
          ))}
        </div>
      </div>
    );
  }
  if (step.type === "run") {
    // Live status line (the Claude subscription brain's "busy" indicator:
    // "⠋ Working… · 12s · 3,240 tokens"). The text carries its own animated
    // braille spinner from the backend ticker, so we just render it; the
    // backend removes the step when the turn ends.
    const text = step.output ? String(step.output) : "";
    if (!text) return null;
    return (
      <div className="status-line status-running">
        <span className="status-text">{text}</span>
      </div>
    );
  }
  if (step.type === "tool") {
    // step.streaming is unreliable for cl.Step — use output presence instead.
    // Output is only written by the backend when the tool finishes.
    const running = !step.output && !step.isError;
    const status: "running" | "ok" | "error" = running
      ? "running"
      : step.isError
        ? "error"
        : "ok";
    const inputChars = step.input ? String(step.input).length : 0;
    const images = elements.filter((e) => e.type === "image");

    if (running) {
      return (
        <div className={`tool-line status-running`}>
          <span className="tool-spinner" />
          <span className="name">{step.name}</span>
          {inputChars > 0 && (
            <span className="extra">{inputChars.toLocaleString()} chars</span>
          )}
        </div>
      );
    }
    return (
      <details className={`tool-line status-${status}`}>
        <summary>
          <span className="sym">{status === "ok" ? "✓" : "✗"}</span>
          <span className="name">{step.name}</span>
        </summary>
        <div className="tool-body">
          {step.input ? <pre>input: {String(step.input)}</pre> : null}
          {step.output ? <Markdown text={String(step.output)} /> : null}
          {images.map((el) => (
            <ElementView key={el.id} element={el} backendOrigin={backendOrigin} />
          ))}
        </div>
      </details>
    );
  }
  return null;
}

function ElementView({
  element,
  backendOrigin,
}: {
  element: IMessageElement;
  backendOrigin: string;
}) {
  if (element.type === "image" && element.url) {
    return (
      <figure className="rich-image">
        <ZoomableImg url={element.url} backendOrigin={backendOrigin} alt={element.name || ""} />
        {element.name ? <figcaption>{element.name}</figcaption> : null}
      </figure>
    );
  }
  if (element.type === "text") {
    // Inline text elements are rendered as code/snippet blocks by Chainlit
    // convention. Markdown gives us code-fence styling for free if the
    // server wraps it; otherwise fall back to <pre>.
    return null;
  }
  return null;
}

// A routine chime arrives as "⏰ <routine> · HH:MM\n<prompt>": show the
// header as a small tag so it reads differently from something typed.
function UserText({ text }: { text: string }) {
  const chime = parseChimeMessage(text);
  if (!chime) return <>{text || null}</>;
  return (
    <>
      <span className="chime-tag" title="Sent by a routine">
        <AlarmGlyph /> {chime.label}
      </span>
      {chime.body}
    </>
  );
}

function AlarmGlyph() {
  return (
    <svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true">
      <circle cx="8" cy="9" r="5" fill="none" stroke="currentColor" strokeWidth="1.5" />
      <path d="M8 6.5V9l1.8 1.2M2.5 3.5l2-1.5M13.5 3.5l-2-1.5" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}
