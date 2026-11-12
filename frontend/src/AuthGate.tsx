// Gate in front of the widget:
//   • server mode, signed out          → "Sign in to continue" (Google | email + password)
//   • site not approved (both modes)   → "Connect this site"
//   • the browser hides which site     → "Voitta can't run here"
//   • otherwise                        → the app, with identity in AuthContext
// Re-probes on focus and every minute, so a disabled account or a removed
// site closes the widget without a reload.

import { useCallback, useEffect, useRef, useState } from "react";
import {
  AuthContext,
  checkAuth,
  logout,
  openApprove,
  openSignIn,
  type AuthState,
} from "./lib/auth";

interface Props {
  backendOrigin: string;
  children: React.ReactNode;
}

const RECHECK_MS = 60_000;

export default function AuthGate({ backendOrigin, children }: Props) {
  const [state, setState] = useState<AuthState | null>(null);
  const [busy, setBusy] = useState(false);

  // Only replace state when something changed, so the periodic re-check
  // doesn't re-render the whole widget every minute.
  const refresh = useCallback(async () => {
    const next = await checkAuth(backendOrigin);
    setState((prev) => (prev && JSON.stringify(prev) === JSON.stringify(next) ? prev : next));
  }, [backendOrigin]);

  useEffect(() => {
    refresh();
    const onFocus = () => {
      if (document.visibilityState === "visible") refresh();
    };
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onFocus);
    const t = window.setInterval(onFocus, RECHECK_MS);
    return () => {
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onFocus);
      clearInterval(t);
    };
  }, [refresh]);

  const run = useCallback(
    async (fn: () => Promise<void>) => {
      setBusy(true);
      try {
        await fn();
        await refresh();
      } finally {
        setBusy(false);
      }
    },
    [refresh],
  );

  // Unapproved site: ask inline (a Voitta frame inside the panel), no popup.
  // Falls back to the Voitta window when the browser can't verify the frame
  // is visible, or the site blocks frames.
  const [inlineFailed, setInlineFailed] = useState(false);

  const onLogout = useCallback(async () => {
    await logout(backendOrigin);
    await refresh();
  }, [backendOrigin, refresh]);

  if (state === null) return null;

  if (state.enabled && !state.authenticated) {
    return (
      <Panel>
        <div style={{ opacity: 0.7, marginBottom: 22 }}>Sign in to continue.</div>
        <PanelButton busy={busy} onClick={() => run(() => openSignIn(backendOrigin, "google"))}>
          <GoogleIcon /> Continue with Google
        </PanelButton>
        <PanelButton busy={busy} onClick={() => run(() => openSignIn(backendOrigin, "password"))}>
          Email and password
        </PanelButton>
        <div style={{ opacity: 0.55, fontSize: 12, marginTop: 18 }}>
          Invite only. Ask your admin for access.
        </div>
      </Panel>
    );
  }

  if (state.site.refused) {
    return (
      <Panel>
        <div style={{ opacity: 0.75 }}>
          Voitta can't run on this page: the browser doesn't say which site it is.
        </div>
      </Panel>
    );
  }

  if (!state.site.approved) {
    const host = (state.site.origin || location.origin).replace(/^https?:\/\//, "");
    if (inlineApprovalSupported() && !inlineFailed) {
      return (
        <Panel>
          <InlineApprove backendOrigin={backendOrigin} onApproved={refresh} onFallback={() => setInlineFailed(true)} />
        </Panel>
      );
    }
    return (
      <Panel>
        <div style={{ opacity: 0.8, marginBottom: 22 }}>
          Voitta isn't connected to <b style={{ wordBreak: "break-all" }}>{host}</b> yet.
        </div>
        <PanelButton busy={busy} primary onClick={() => run(() => openApprove(backendOrigin))}>
          Connect this site
        </PanelButton>
      </Panel>
    );
  }

  return (
    <AuthContext.Provider
      value={{
        enabled: state.enabled,
        email: state.email,
        role: state.role ?? null,
        isAdmin: !!state.is_admin,
        hasPassword: !!state.has_password,
        logout: onLogout,
        refresh,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

// Inline approval needs IntersectionObserver v2 (Chrome/Edge): the Voitta
// frame uses it to refuse clicks while the page hides or covers it.
export function inlineApprovalSupported(): boolean {
  return typeof IntersectionObserverEntry !== "undefined" && "isVisible" in IntersectionObserverEntry.prototype;
}

const INLINE_READY_MS = 6000;

// The "Allow Voitta on <site>?" frame, served by the backend
// (/auth/approve-embed). The page can't read or click into it; it tells us
// when the site was approved, or that we should fall back to the window.
function InlineApprove({
  backendOrigin,
  onApproved,
  onFallback,
}: {
  backendOrigin: string;
  onApproved: () => void;
  onFallback: () => void;
}) {
  const frame = useRef<HTMLIFrameElement | null>(null);
  // Latest callbacks without re-running the effect (that would restart the
  // ready timer after the frame already said hello).
  const cb = useRef({ onApproved, onFallback });
  cb.current = { onApproved, onFallback };
  useEffect(() => {
    const origin = new URL(backendOrigin).origin;
    let ready = false;
    const onMsg = (e: MessageEvent) => {
      if (e.origin !== origin || !frame.current || e.source !== frame.current.contentWindow) return;
      const t = (e.data as { t?: string } | null)?.t;
      if (t === "voitta-embed-ready") ready = true;
      else if (t === "voitta-site-approved") cb.current.onApproved();
      else if (t === "voitta-embed-unsupported" || t === "voitta-embed-signin") cb.current.onFallback();
    };
    window.addEventListener("message", onMsg);
    // Frame blocked (the site's CSP, local-network rules…): use the window.
    const timer = window.setTimeout(() => {
      if (!ready) cb.current.onFallback();
    }, INLINE_READY_MS);
    return () => {
      window.removeEventListener("message", onMsg);
      clearTimeout(timer);
    };
  }, [backendOrigin]);
  return (
    <iframe
      ref={frame}
      src={`${backendOrigin}/auth/approve-embed`}
      title="Allow Voitta on this site"
      style={{ width: 300, maxWidth: "100%", height: 190, border: "1px solid var(--voitta-border)", borderRadius: 8 }}
    />
  );
}

// Docked panel mirroring the drawer's geometry — confined to the widget's
// pane, NOT a full-page overlay. The shadow host is pointer-events:none; this
// panel re-enables them for itself.
function Panel({ children }: { children: React.ReactNode }) {
  return (
    <div className="root" data-theme="dark">
      <div
        style={{
          position: "fixed",
          top: 0,
          right: 0,
          height: "100vh",
          width: "min(420px, 92vw)",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          background: "var(--voitta-bg)",
          color: "var(--voitta-text)",
          borderLeft: "1px solid var(--voitta-border)",
          boxShadow: "var(--voitta-shadow)",
          font: "14px system-ui, sans-serif",
          pointerEvents: "auto",
          zIndex: 2147483647,
        }}
      >
        <div style={{ textAlign: "center", width: 280, padding: "0 24px" }}>
          <div style={{ fontSize: 22, fontWeight: 700, marginBottom: 6 }}>Voitta</div>
          {children}
        </div>
      </div>
    </div>
  );
}

function PanelButton({
  children,
  onClick,
  busy,
  primary,
}: {
  children: React.ReactNode;
  onClick: () => void;
  busy: boolean;
  primary?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={busy}
      style={{
        display: "flex",
        width: "100%",
        alignItems: "center",
        justifyContent: "center",
        gap: 10,
        padding: "10px 18px",
        marginTop: 10,
        borderRadius: 8,
        border: primary ? "1px solid #3b82f6" : "1px solid #2a3548",
        background: busy ? "#1f2733" : primary ? "#3b82f6" : "#f1f5f9",
        color: busy ? "#a0a3ad" : primary ? "#ffffff" : "#0f172a",
        fontWeight: 600,
        cursor: busy ? "default" : "pointer",
      }}
    >
      {busy ? "Waiting for the Voitta window…" : children}
    </button>
  );
}

function GoogleIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 48 48" aria-hidden="true">
      <path fill="#EA4335" d="M24 9.5c3.5 0 6.6 1.2 9 3.6l6.7-6.7C35.6 2.6 30.2 0 24 0 14.6 0 6.4 5.4 2.5 13.2l7.8 6.1C12.2 13.3 17.6 9.5 24 9.5z" />
      <path fill="#4285F4" d="M46.5 24.5c0-1.6-.1-3.2-.4-4.7H24v9h12.7c-.6 3-2.3 5.5-4.8 7.2l7.4 5.7c4.3-4 6.8-9.9 6.8-17.2z" />
      <path fill="#FBBC05" d="M10.3 19.3l-7.8-6.1C.9 16.5 0 20.1 0 24s.9 7.5 2.5 10.8l7.8-6.1c-.5-1.5-.8-3-.8-4.7s.3-3.2.8-4.7z" />
      <path fill="#34A853" d="M24 48c6.2 0 11.5-2 15.3-5.5l-7.4-5.7c-2 1.4-4.7 2.3-7.9 2.3-6.4 0-11.8-3.8-13.7-9.3l-7.8 6.1C6.4 42.6 14.6 48 24 48z" />
    </svg>
  );
}
