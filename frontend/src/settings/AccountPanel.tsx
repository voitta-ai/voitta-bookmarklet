// Settings → Account (everyone). Server: who you are, how you can sign in,
// change password (opens our own page — never typed into the host page),
// sign out. Both modes: the sites Voitta may run on, with Remove.

import { useCallback, useEffect, useState } from "react";
import { openChangePassword, useAuth } from "../lib/auth";

interface Site {
  origin: string;
  added_at: number;
}

interface Account {
  server: boolean;
  email?: string;
  role?: string;
  has_password?: boolean;
  has_google?: boolean;
  sites: Site[];
}

export default function AccountPanel({ backendOrigin }: { backendOrigin: string }) {
  const auth = useAuth();
  const [acct, setAcct] = useState<Account | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const r = await fetch(`${backendOrigin}/api/account`, { credentials: "include" });
      if (!r.ok) throw new Error(`status ${r.status}`);
      setAcct((await r.json()) as Account);
      setErr(null);
    } catch (e) {
      setErr(String(e));
    }
  }, [backendOrigin]);

  useEffect(() => {
    load();
  }, [load]);

  async function remove(origin: string) {
    const here = origin === location.origin;
    const r = await fetch(`${backendOrigin}/api/account/sites?origin=${encodeURIComponent(origin)}`, {
      method: "DELETE",
      credentials: "include",
    });
    if (!r.ok) {
      setErr(`Could not remove ${origin} (status ${r.status}).`);
      return;
    }
    if (here) {
      await auth.refresh(); // this page just lost access: the gate takes over
      return;
    }
    load();
  }

  if (!acct) return <p className="muted">{err ?? "Loading…"}</p>;

  return (
    <div className="account-panel" style={{ display: "grid", gap: 16 }}>
      {acct.server ? (
        <section>
          <div style={{ display: "flex", justifyContent: "space-between", gap: 8, alignItems: "baseline" }}>
            <b style={{ wordBreak: "break-all" }}>{acct.email}</b>
            <span className="muted">{acct.role}</span>
          </div>
          <div className="muted" style={{ marginTop: 4 }}>
            Sign-in: Google {acct.has_google ? "✓" : "–"} · password {acct.has_password ? "✓" : "–"}
          </div>
          <div style={{ display: "flex", gap: 8, marginTop: 10, flexWrap: "wrap" }}>
            <button type="button" className="secondary-btn" onClick={() => openChangePassword(backendOrigin).then(load)}>
              {acct.has_password ? "Change password ↗" : "Set a password ↗"}
            </button>
            <button type="button" className="secondary-btn" onClick={() => auth.logout()}>
              Sign out
            </button>
          </div>
        </section>
      ) : (
        <p className="muted">This app runs on your computer and has no account.</p>
      )}

      <section>
        <div style={{ fontWeight: 600, marginBottom: 6 }}>Sites where Voitta can run</div>
        {acct.sites.length === 0 && <p className="muted">None yet. Open Voitta on a site and click Connect.</p>}
        {acct.sites.map((s) => (
          <div
            key={s.origin}
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: 8,
              padding: "6px 0",
              borderBottom: "1px solid var(--voitta-border)",
            }}
          >
            <span style={{ fontFamily: "ui-monospace, Menlo, monospace", fontSize: 12, wordBreak: "break-all" }}>
              {s.origin.replace(/^https?:\/\//, "")}
              {s.origin === location.origin && <span className="muted"> (this page)</span>}
            </span>
            <button type="button" className="secondary-btn" onClick={() => remove(s.origin)}>
              Remove
            </button>
          </div>
        ))}
      </section>
      {err && <div className="status err">{err}</div>}
    </div>
  );
}
