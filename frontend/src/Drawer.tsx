// Drawer chrome: the floating handle (visible when closed), the
// sliding panel, the header with brand + provider chip + button tray,
// and view switching (chat / settings). Layout (left/right) comes from
// the persisted settings cache.

import { useCallback, useEffect, useRef, useState } from "react";
import { useSetRecoilState } from "recoil";
import { threadIdToResumeState, useChatInteract } from "@chainlit/react-client";
import { useSettings } from "./lib/useSettings";
import { saveSettings, AGENT_SDK_PROVIDER } from "./lib/settings";
import { useAuth } from "./lib/auth";
import { DOG_ICON_DATA_URI } from "./lib/dogIcon";
import ChatPane from "./ChatPane";
import SettingsView from "./SettingsView";
import CallFnRouter from "./lib/CallFnRouter";
import ReportPane from "./report/ReportPane";
import ThreadPicker from "./ThreadPicker";
import ProjectChip from "./ProjectChip";
import { activeTabState, reportCollapsedState, workspaceTabOpenState } from "./report/state";

type View = "chat" | "settings";
type ResolvedTheme = "light" | "dark";

// Resolve the persisted theme ("light" | "dark" | "auto") to a concrete
// light/dark. We push the resolved value onto data-theme so the DOM never
// carries "auto": the themed CSS then only needs its two complete token
// blocks ([data-theme="light"] / ["dark"]), instead of relying on the
// prefers-color-scheme fallback block — which had drifted out of sync and
// left header text dark-on-dark in auto mode on OS-dark machines.
function resolveTheme(theme: string): ResolvedTheme {
  if (theme === "auto") {
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  return theme === "dark" ? "dark" : "light";
}

const STORE_OPEN = "voitta-bkmk-open";
const STORE_WIDTH = "voitta-bkmk-width";
const DEFAULT_WIDTH_PX = 420;
const MIN_WIDTH_PX = 280;
const MAX_WIDTH_VW = 92;

// Provider chip shows a single letter rather than the full id.
const PROVIDER_LETTER: Record<string, string> = {
  anthropic: "A",
  gemini: "G",
  openai: "O",
  codex: "X",
  claude_code: "C",
};

function providerLetter(provider: string | undefined): string {
  if (!provider) return "—";
  return PROVIDER_LETTER[provider.toLowerCase()] ?? provider[0].toUpperCase();
}

// Up-to-two capital initials for the avatar: from a dotted/underscored email
// local part ("roman.semein" -> "RS"), else the first letter ("roman" -> "R").
// (We only have the email — Google's identifier — so there's no separate
// display name to draw real profile initials from.)
function avatarInitials(email: string): string {
  const local = (email.split("@")[0] || email).trim();
  const parts = local.split(/[._+-]+/).filter(Boolean);
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
  return (local[0] || "?").toUpperCase();
}

// Logged-in account avatar + logout dropdown (server mode only — hidden when
// no email). The circle replaces the full-width email in the bar to save space;
// the email moves into the dropdown.
function UserMenu({ email, onLogout }: { email: string; onLogout: () => void }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="user-menu">
      <button
        className="user-menu-btn"
        type="button"
        title={email}
        aria-label="Account menu"
        onClick={() => setOpen((o) => !o)}
      >
        <span className="user-menu-avatar">{avatarInitials(email)}</span>
      </button>
      {open && (
        <>
          <div className="user-menu-backdrop" onClick={() => setOpen(false)} />
          <div className="user-menu-menu">
            <div className="user-menu-account" title={email}>{email}</div>
            <button
              className="user-menu-item"
              type="button"
              onClick={() => { setOpen(false); onLogout(); }}
            >
              Log out
            </button>
          </div>
        </>
      )}
    </div>
  );
}

function clampWidth(px: number): number {
  const max = Math.floor(window.innerWidth * (MAX_WIDTH_VW / 100));
  return Math.max(MIN_WIDTH_PX, Math.min(max, px));
}

function loadInitialWidth(): number {
  try {
    const saved = parseInt(localStorage.getItem(STORE_WIDTH) || "", 10);
    if (Number.isFinite(saved) && saved > 0) return clampWidth(saved);
  } catch {
    /* ignore */
  }
  return DEFAULT_WIDTH_PX;
}

interface Props {
  backendOrigin: string;
}

export default function Drawer({ backendOrigin }: Props) {
  const settings = useSettings();
  const auth = useAuth();

  // Brand collapse: swap the "Voitta" wordmark for the dog mascot the moment
  // the header content would overflow — i.e. as soon as components begin to
  // squash. Measured, not a fixed px threshold, because the squash point
  // depends on the (variable-length) account email. While expanded the
  // shrinkable items (email, thread picker) are pinned to natural width via
  // [data-brand] so overflow reflects true content; once collapsed they're
  // allowed to ellipsis. Hysteresis (remember the collapse width + a buffer)
  // prevents flip-flop at the boundary.
  const headerRef = useRef<HTMLElement | null>(null);
  const [brandCollapsed, setBrandCollapsed] = useState(false);
  const collapseWidthRef = useRef<number>(0);

  useEffect(() => {
    const el = headerRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    let raf = 0;
    const measure = () => {
      raf = 0;
      const overflowing = el.scrollWidth > el.clientWidth + 1;
      setBrandCollapsed((collapsed) => {
        if (!collapsed) {
          if (overflowing) {
            collapseWidthRef.current = el.clientWidth;
            return true;
          }
          return false;
        }
        // Collapsed: re-expand only once clearly wider than where we collapsed.
        if (el.clientWidth > collapseWidthRef.current + 16) return false;
        return true;
      });
    };
    const ro = new ResizeObserver(() => {
      if (!raf) raf = requestAnimationFrame(measure);
    });
    ro.observe(el);
    measure();
    return () => {
      ro.disconnect();
      if (raf) cancelAnimationFrame(raf);
    };
  }, []);

  const setWorkspaceOpen = useSetRecoilState(workspaceTabOpenState);
  const setActiveTab = useSetRecoilState(activeTabState);
  const setReportCollapsed = useSetRecoilState(reportCollapsedState);

  // selectedThreadId drives which thread ChatPane mounts into.
  // null = new/current session. Changing it re-keys ChatPane so it remounts
  // cleanly (same pattern as stirista-conversational-agent).
  const [selectedThreadId, setSelectedThreadId] = useState<string | null>(null);
  const [newConvKey, setNewConvKey] = useState(0);
  // Brain (subscription) mode: the SDK session whose transcript the pane should
  // replay. Distinct from selectedThreadId (a Chainlit thread) — SDK sessions
  // live in the Agent SDK's own store and are fetched by ChatPane, not resumed
  // through Chainlit. null = fresh conversation.
  const [sdkResumeId, setSdkResumeId] = useState<string | null>(null);
  const setThreadIdToResume = useSetRecoilState(threadIdToResumeState);
  const { clear } = useChatInteract();

  const [open, setOpen] = useState<boolean>(() => {
    try {
      return sessionStorage.getItem(STORE_OPEN) !== "0";
    } catch {
      return true;
    }
  });
  // "Ready" = the selected brain can run. API providers need a saved key; the
  // subscription brain just needs the engine installed (its token is collected
  // in-chat on first use), so it's ready whenever it's available.
  const isBrain = settings.provider === AGENT_SDK_PROVIDER;
  const currentHasKey = isBrain
    ? settings.agent_sdk.available
    : Boolean(settings.has_api_keys[settings.provider]);
  const [view, setView] = useState<View>(currentHasKey ? "chat" : "settings");

  // Effective light/dark. Recomputed when the persisted theme changes, and —
  // while in "auto" — kept in sync with the OS scheme via a matchMedia listener
  // so the widget flips live without a reload.
  const [effectiveTheme, setEffectiveTheme] = useState<ResolvedTheme>(() =>
    resolveTheme(settings.theme),
  );
  useEffect(() => {
    if (settings.theme !== "auto") {
      setEffectiveTheme(settings.theme === "dark" ? "dark" : "light");
      return;
    }
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const apply = () => setEffectiveTheme(mq.matches ? "dark" : "light");
    apply();
    mq.addEventListener("change", apply);
    return () => mq.removeEventListener("change", apply);
  }, [settings.theme]);
  const [width, setWidth] = useState<number>(loadInitialWidth);
  const [resizing, setResizing] = useState(false);
  const widthRef = useRef(width);
  widthRef.current = width;
  const layoutRef = useRef(settings.layout);
  layoutRef.current = settings.layout;

  useEffect(() => {
    try {
      sessionStorage.setItem(STORE_OPEN, open ? "1" : "0");
    } catch {
      /* ignore */
    }
  }, [open]);

  useEffect(() => {
    try {
      localStorage.setItem(STORE_WIDTH, String(width));
    } catch {
      /* ignore */
    }
  }, [width]);

  // Re-clamp on viewport resize so width never exceeds 92vw after the
  // window shrinks.
  useEffect(() => {
    const onWindowResize = () => setWidth((w) => clampWidth(w));
    window.addEventListener("resize", onWindowResize);
    return () => window.removeEventListener("resize", onWindowResize);
  }, []);


  // Pointer-driven drag. The resizer sits on the inner edge of the
  // drawer — left edge in chat-right layout, right edge in chat-left.
  // Drag direction is inverted accordingly. Pointer capture lets us
  // keep tracking even if the cursor leaves the 6px grip strip.
  const onResizeDown = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    e.preventDefault();
    const target = e.currentTarget;
    target.setPointerCapture(e.pointerId);
    setResizing(true);
    const startX = e.clientX;
    const startW = widthRef.current;
    const chatLeft = layoutRef.current === "chat-left";

    const move = (ev: PointerEvent) => {
      const dx = ev.clientX - startX;
      setWidth(clampWidth(chatLeft ? startW + dx : startW - dx));
    };
    const up = (ev: PointerEvent) => {
      setResizing(false);
      try {
        target.releasePointerCapture(ev.pointerId);
      } catch {
        /* ignore */
      }
      target.removeEventListener("pointermove", move);
      target.removeEventListener("pointerup", up);
      target.removeEventListener("pointercancel", up);
    };
    target.addEventListener("pointermove", move);
    target.addEventListener("pointerup", up);
    target.addEventListener("pointercancel", up);
  }, []);

  const onResizeDblClick = useCallback(() => {
    setWidth(DEFAULT_WIDTH_PX);
  }, []);

  function toggleView(target: View) {
    setView((v) => (v === target ? "chat" : target));
  }

  const providerChip = providerLetter(settings.provider);

  return (
    <div
      className="root"
      data-open={open ? "true" : "false"}
      data-resizing={resizing ? "true" : "false"}
      data-layout={settings.layout}
      data-theme={effectiveTheme}
      style={{ ["--voitta-pane-width" as string]: width + "px" } as React.CSSProperties}
    >
      {/* floating handle — only visible when drawer is closed */}
      <button
        className="handle"
        type="button"
        title="Open chat"
        aria-label="Open chat"
        onClick={() => setOpen(true)}
      >
        <svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true">
          <path
            d="M4 4h16v12H7l-3 3V4z"
            fill="none"
            stroke="currentColor"
            strokeWidth={2}
            strokeLinejoin="round"
          />
        </svg>
      </button>

      <aside className="drawer" role="complementary" aria-label="Voitta chat">
        <div
          className="resizer"
          role="separator"
          aria-orientation="vertical"
          title="Drag to resize · double-click to reset"
          onPointerDown={onResizeDown}
          onDoubleClick={onResizeDblClick}
        />
        <header ref={headerRef} data-brand={brandCollapsed ? "dog" : "name"}>
          {/* Wordmark until the header would overflow, then the dog mascot.
              data-brand also flips which items may shrink (see header.css). */}
          {brandCollapsed ? (
            <img className="brand-dog" src={DOG_ICON_DATA_URI} alt="Voitta" title="Voitta" />
          ) : (
            <span className="brand-name">Voitta</span>
          )}
          <button
            className="provider-chip"
            type="button"
            title="Settings"
            onClick={() => toggleView("settings")}
          >
            {providerChip}
          </button>
          <ThreadPicker
            backendOrigin={backendOrigin}
            selectedThreadId={selectedThreadId}
            onThreadSelect={(id) => {
              // Set idToResume BEFORE key changes so ChatPane mounts with correct atom value.
              if (id === null) { clear(); setThreadIdToResume(undefined); setNewConvKey((k) => k + 1); }
              else { setThreadIdToResume(id as any); }
              setSelectedThreadId(id);
            }}
            onSdkSelect={(sessionId) => {
              // SDK session pick: reset to a clean Chainlit pane and let ChatPane
              // replay the transcript. clear() wipes the current messages so the
              // old conversation doesn't flash before the fetch resolves; the key
              // bump forces a fresh mount (and socket) for the resumed session.
              clear();
              setThreadIdToResume(undefined);
              setSelectedThreadId(null);
              setSdkResumeId(sessionId);
              setNewConvKey((k) => k + 1);
            }}
          />
          <span className="spacer" />
          {auth.email && <UserMenu email={auth.email} onLogout={auth.logout} />}
          {view === "chat" && (
            <>
              <ProjectChip
                backendOrigin={backendOrigin}
                onSwitched={() => {
                  // Same gesture as "+ new conversation": the new project's
                  // thread history + workspace mount fresh. Project is fixed
                  // per thread — a conversation never straddles projects.
                  clear();
                  setThreadIdToResume(undefined);
                  setSelectedThreadId(null);
                  setNewConvKey((k) => k + 1);
                  window.dispatchEvent(new Event("voitta-project-switched"));
                }}
              />
              <button
                className="hbtn"
                type="button"
                title="Workspace"
                aria-label="Open workspace"
                onClick={() => { setWorkspaceOpen(true); setActiveTab("workspace"); setReportCollapsed(false); }}
              >
                <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
                  <path d="M1 4h5l1.5 1.5H15V13H1V4z" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
                </svg>
              </button>
              <button
                className="hbtn theme-toggle"
                type="button"
                title={effectiveTheme === "dark" ? "Switch to light" : "Switch to dark"}
                aria-label="Toggle color scheme"
                onClick={() => {
                  saveSettings(backendOrigin, { theme: effectiveTheme === "dark" ? "light" : "dark" });
                }}
              >
                {(() => {
                  return effectiveTheme === "dark"
                    ? (
                      // sun
                      <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                        <circle cx="8" cy="8" r="3" />
                        <line x1="8" y1="1" x2="8" y2="3" />
                        <line x1="8" y1="13" x2="8" y2="15" />
                        <line x1="1" y1="8" x2="3" y2="8" />
                        <line x1="13" y1="8" x2="15" y2="8" />
                        <line x1="2.93" y1="2.93" x2="4.34" y2="4.34" />
                        <line x1="11.66" y1="11.66" x2="13.07" y2="13.07" />
                        <line x1="13.07" y1="2.93" x2="11.66" y2="4.34" />
                        <line x1="4.34" y1="11.66" x2="2.93" y2="13.07" />
                      </svg>
                    )
                    : (
                      // moon
                      <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                        <path d="M13.5 10A6 6 0 0 1 6 2.5a5.5 5.5 0 1 0 7.5 7.5z" />
                      </svg>
                    );
                })()}
              </button>
            </>
          )}
          <button
            className={`hbtn ${view === "settings" ? "active" : ""}`}
            type="button"
            title={view === "settings" ? "Back to chat" : "Settings"}
            aria-label={view === "settings" ? "Back to chat" : "Settings"}
            onClick={() => toggleView("settings")}
          >
            {view === "settings" ? "←" : "⚙"}
          </button>
          <button
            className="hbtn"
            type="button"
            title="Close"
            aria-label="Close"
            onClick={() => setOpen(false)}
          >
            ×
          </button>
        </header>

        {/* Key forces a clean remount on thread switches or new-conversation. */}
        <div className="chat-wrap" style={view !== "chat" ? { display: "none" } : undefined}>
          <ChatPane
            key={selectedThreadId ?? `new-${newConvKey}`}
            backendOrigin={backendOrigin}
            hasApiKey={currentHasKey}
            threadId={selectedThreadId}
            sdkResumeSessionId={sdkResumeId}
          />
        </div>
        {view === "settings" && (
          <SettingsView backendOrigin={backendOrigin} onClose={() => setView("chat")} />
        )}
      </aside>

      <ReportPane
        backendOrigin={backendOrigin}
        chatOpen={open}
        onResizeDown={onResizeDown}
        onResizeDblClick={onResizeDblClick}
      />
      <CallFnRouter />
    </div>
  );
}
