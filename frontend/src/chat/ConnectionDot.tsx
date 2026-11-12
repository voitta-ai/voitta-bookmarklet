// Header dot for the browser ↔ Voitta socket, so a dropped connection isn't
// mistaken for a site logout during a demo:
//   green  connected      amber  reconnecting (just dropped)      red  lost (> LOST_MS)
// Before the first connect there's nothing to report, so it stays grey.

import { useChatData } from "@chainlit/react-client";
import { useEffect, useRef, useState } from "react";

const LOST_MS = 15_000;

type Conn = "idle" | "ok" | "reconnecting" | "lost";

export function connLabel(c: Conn, since: number): string {
  const t = new Date(since).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  switch (c) {
    case "ok":
      return `Connected to Voitta · since ${t}`;
    case "reconnecting":
      return `Connection to Voitta dropped at ${t} · reconnecting…`;
    case "lost":
      return `No connection to Voitta since ${t}. This is Voitta, not the website you're on.`;
    default:
      return "Connecting to Voitta…";
  }
}

export default function ConnectionDot() {
  const { connected } = useChatData();
  const [conn, setConn] = useState<Conn>("idle");
  const [since, setSince] = useState(() => Date.now());
  const everConnected = useRef(false);

  useEffect(() => {
    if (connected) {
      everConnected.current = true;
      setConn("ok");
      setSince(Date.now());
      return;
    }
    if (!everConnected.current) return;
    setConn("reconnecting");
    setSince(Date.now());
    const t = window.setTimeout(() => setConn("lost"), LOST_MS);
    return () => window.clearTimeout(t);
  }, [connected]);

  const label = connLabel(conn, since);
  return <span className={`conn-dot ${conn}`} role="status" aria-label={label} title={label} />;
}
