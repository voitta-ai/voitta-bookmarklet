// Sign-in + site-approval client. Mirrors the backend /api/auth/* routes.
//
// Server mode: invite-only accounts (Google or password, same email). Both
// modes: the widget only works on sites the user has approved — the backend
// refuses other sites, and /api/auth/me tells us which case we're in.
//
// Everything that asks for a password or a "yes" happens in a popup on the
// backend's own origin (/auth/signin, /auth/approve, /auth/password), never
// inside the host page, which can read the widget's DOM.

import { createContext, useContext } from "react";

export interface SiteState {
  origin: string | null; // this page's origin when it isn't one of ours
  approved: boolean;
  refused: boolean; // the browser didn't say which site this is (opaque origin)
}

export interface AuthState {
  enabled: boolean; // server mode (accounts)
  authenticated: boolean;
  email: string | null; // only revealed to approved sites
  role?: "owner" | "admin" | "user";
  is_admin?: boolean;
  has_password?: boolean;
  has_google?: boolean;
  site: SiteState;
}

const OPEN: AuthState = {
  enabled: false,
  authenticated: false,
  email: null,
  site: { origin: null, approved: true, refused: false },
};

// Identity + actions exposed to the app tree by AuthGate. On desktop
// enabled=false and email=null, so consumers hide the account UI.
export interface AuthInfo {
  enabled: boolean;
  email: string | null;
  role: "owner" | "admin" | "user" | null;
  isAdmin: boolean;
  hasPassword: boolean;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
}

export const AuthContext = createContext<AuthInfo>({
  enabled: false,
  email: null,
  role: null,
  isAdmin: false,
  hasPassword: false,
  logout: async () => {},
  refresh: async () => {},
});

export const useAuth = () => useContext(AuthContext);

export async function checkAuth(backendOrigin: string): Promise<AuthState> {
  try {
    const res = await fetch(`${backendOrigin}/api/auth/me`, { credentials: "include" });
    if (res.ok) {
      const body = (await res.json()) as Partial<AuthState>;
      return { ...OPEN, ...body, site: { ...OPEN.site, ...(body.site ?? {}) } } as AuthState;
    }
  } catch (err) {
    console.warn("[voitta] auth check failed", err);
  }
  // If the probe itself fails, fail open to the normal UI rather than
  // hard-locking it — guarded endpoints still refuse on their own.
  return OPEN;
}

export async function logout(backendOrigin: string): Promise<void> {
  try {
    await fetch(`${backendOrigin}/api/auth/logout`, { method: "POST", credentials: "include" });
  } catch (err) {
    console.warn("[voitta] logout failed", err);
  }
}

// Open a popup on the backend and resolve when it's done: it posts one of
// `doneMessages` to us, or (fallback, e.g. after Google's COOP severs the
// opener) the user closes it. `onMessage` may answer handshakes.
function popup(
  backendOrigin: string,
  path: string,
  name: string,
  isDone: (data: unknown) => boolean,
  onMessage?: (e: MessageEvent, win: Window) => void,
): Promise<void> {
  return new Promise((resolve) => {
    const win = window.open(`${backendOrigin}${path}`, name, "width=480,height=640");
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      window.removeEventListener("message", onMsg);
      clearInterval(poll);
      resolve();
    };
    const onMsg = (e: MessageEvent) => {
      if (!win || e.source !== win || e.origin !== new URL(backendOrigin).origin) return;
      onMessage?.(e, win);
      if (isDone(e.data)) finish();
    };
    window.addEventListener("message", onMsg);
    const poll = window.setInterval(() => {
      if (!win || win.closed) finish();
    }, 600);
  });
}

export function openSignIn(backendOrigin: string, how: "google" | "password"): Promise<void> {
  const path = how === "google" ? "/api/auth/google/start" : "/auth/signin";
  return popup(backendOrigin, path, "voitta-signin", (d) => d === "voitta-auth");
}

// The approve page learns which site to allow from OUR postMessage's
// browser-stamped origin (e.origin on its side) — it never trusts a URL
// parameter — so we just answer its hello.
export function openApprove(backendOrigin: string): Promise<void> {
  const target = new URL(backendOrigin).origin;
  return popup(
    backendOrigin,
    "/auth/approve",
    "voitta-approve",
    (d) => !!d && typeof d === "object" && (d as { t?: string }).t === "voitta-site-approved",
    (e, win) => {
      const d = e.data as { t?: string } | null;
      if (d && d.t === "voitta-approve-hello") win.postMessage({ t: "voitta-approve-origin" }, target);
    },
  );
}

export function openChangePassword(backendOrigin: string): Promise<void> {
  return popup(backendOrigin, "/auth/password", "voitta-password", (d) => d === "voitta-password-changed");
}
