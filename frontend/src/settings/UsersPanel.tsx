// Settings → Users (admins, server mode). Invite people, set roles and
// passwords, unlock, sign out everywhere, disable/enable; recent activity.
// Disabled users are listed last and keep their data; Enable restores them.

import { useCallback, useEffect, useMemo, useState } from "react";

interface User {
  email: string;
  role: "owner" | "admin" | "user";
  has_password: boolean;
  has_google: boolean;
  disabled: boolean;
  disabled_at: number | null;
  disabled_by: string | null;
  locked_minutes: number;
  last_seen: number | null;
}

interface Activity {
  id: number;
  at: number;
  actor: string | null;
  action: string;
  target: string | null;
  ok: boolean;
  detail: Record<string, unknown> | null;
  ip: string | null;
}

const DEFAULT_PASSWORD = "314159";

function ago(ts: number | null): string {
  if (!ts) return "never";
  const s = Date.now() / 1000 - ts;
  if (s < 120) return "now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export default function UsersPanel({ backendOrigin }: { backendOrigin: string }) {
  const [list, setList] = useState<User[] | null>(null);
  const [me, setMe] = useState("");
  const [q, setQ] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [inviting, setInviting] = useState(false);
  const [menuFor, setMenuFor] = useState<string | null>(null);
  const [pwFor, setPwFor] = useState<string | null>(null);

  const api = useCallback(
    async (path: string, body?: unknown) => {
      const r = await fetch(`${backendOrigin}/api/admin${path}`, {
        method: body === undefined ? "GET" : "POST",
        credentials: "include",
        headers: body === undefined ? undefined : { "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      const b = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error((b as { detail?: string }).detail || `status ${r.status}`);
      if ((b as { users?: User[] }).users) setList((b as { users: User[] }).users);
      return b as Record<string, unknown>;
    },
    [backendOrigin],
  );

  useEffect(() => {
    api("/users")
      .then((b) => setMe(String(b.me || "")))
      .catch((e) => setErr(String(e.message || e)));
  }, [api]);

  async function act(action: string, body: Record<string, unknown>, done?: string) {
    setErr(null);
    setNote(null);
    setMenuFor(null);
    try {
      const b = await api(`/users/${action}`, body);
      if (done) setNote(done);
      return b;
    } catch (e) {
      setErr(String((e as Error).message || e));
      return null;
    }
  }

  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return (list ?? []).filter((u) => !needle || u.email.includes(needle));
  }, [list, q]);
  const active = shown.filter((u) => !u.disabled);
  const disabled = shown.filter((u) => u.disabled);

  if (list === null) return <p className="muted">{err ?? "Loading users…"}</p>;

  return (
    <div className="users-panel" style={{ display: "grid", gap: 10 }}>
      <div style={{ display: "flex", gap: 8 }}>
        <input
          type="search"
          placeholder="search email…"
          value={q}
          onChange={(e) => setQ(e.currentTarget.value)}
          style={{ flex: 1, minWidth: 0 }}
        />
        <button type="button" className="secondary-btn" onClick={() => setInviting((v) => !v)}>
          {inviting ? "Close" : "+ Invite"}
        </button>
      </div>

      {inviting && (
        <InviteForm
          api={api}
          onDone={(msg) => {
            setNote(msg);
          }}
        />
      )}

      {err && <div className="status err">{err}</div>}
      {note && <div className="status">{note}</div>}

      <div>
        {active.map((u) => (
          <UserRow
            key={u.email}
            u={u}
            isMe={u.email === me}
            menuOpen={menuFor === u.email}
            onMenu={() => setMenuFor(menuFor === u.email ? null : u.email)}
            act={act}
            onSetPassword={() => {
              setMenuFor(null);
              setPwFor(u.email);
            }}
          >
            {pwFor === u.email && (
              <SetPasswordForm
                email={u.email}
                act={act}
                api={api}
                onClose={() => setPwFor(null)}
              />
            )}
          </UserRow>
        ))}
        {disabled.length > 0 && (
          <div className="muted" style={{ margin: "14px 0 4px", fontSize: 12, letterSpacing: ".04em" }}>
            ─── Disabled ───
          </div>
        )}
        {disabled.map((u) => (
          <div key={u.email} style={rowStyle(true)}>
            <div style={{ minWidth: 0 }}>
              <div style={{ wordBreak: "break-all" }}>{u.email}</div>
              <div className="muted" style={{ fontSize: 12 }}>
                disabled {ago(u.disabled_at)}
                {u.disabled_by ? ` by ${u.disabled_by.split("@")[0]}` : ""}
              </div>
            </div>
            <button type="button" className="secondary-btn" onClick={() => act("enable", { email: u.email }, `${u.email} enabled.`)}>
              Enable
            </button>
          </div>
        ))}
      </div>

      <ActivityLog api={api} />
    </div>
  );
}

function rowStyle(dim = false): React.CSSProperties {
  return {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    gap: 8,
    padding: "8px 0",
    borderBottom: "1px solid var(--voitta-border)",
    opacity: dim ? 0.7 : 1,
    position: "relative",
  };
}

function UserRow({
  u,
  isMe,
  menuOpen,
  onMenu,
  act,
  onSetPassword,
  children,
}: {
  u: User;
  isMe: boolean;
  menuOpen: boolean;
  onMenu: () => void;
  act: (action: string, body: Record<string, unknown>, done?: string) => Promise<unknown>;
  onSetPassword: () => void;
  children?: React.ReactNode;
}) {
  const how = [u.has_google && "Google", u.has_password && "password"].filter(Boolean).join(" · ") || "invited";
  const owner = u.role === "owner";
  const items: { label: string; run: () => void }[] = [];
  if (!owner) {
    items.push(
      u.role === "admin"
        ? { label: "Make user", run: () => act("role", { email: u.email, role: "user" }, `${u.email} is now a user.`) }
        : { label: "Make admin", run: () => act("role", { email: u.email, role: "admin" }, `${u.email} is now an admin.`) },
    );
  }
  items.push({ label: "Set password…", run: onSetPassword });
  if (u.has_password)
    items.push({ label: "Remove password", run: () => act("password", { email: u.email, mode: "remove" }, "Password removed.") });
  if (u.locked_minutes) items.push({ label: "Unlock", run: () => act("unlock", { email: u.email }, `${u.email} unlocked.`) });
  items.push({
    label: "Sign out everywhere",
    run: () => act("sign-out", { email: u.email }, `${u.email} was signed out everywhere.`),
  });
  if (!owner && !isMe)
    items.push({ label: "Disable", run: () => act("disable", { email: u.email }, `${u.email} disabled.`) });

  return (
    <div>
      <div style={rowStyle()}>
        <div style={{ minWidth: 0 }}>
          <div style={{ display: "flex", gap: 8, alignItems: "baseline" }}>
            <span style={{ wordBreak: "break-all" }}>{u.email}</span>
            <span className="muted" style={{ fontSize: 12 }}>{u.role}{isMe ? " (you)" : ""}</span>
          </div>
          <div className="muted" style={{ fontSize: 12 }}>
            {how}
            {u.locked_minutes ? ` · 🔒 ${u.locked_minutes} min` : ""} · seen {ago(u.last_seen)}
          </div>
        </div>
        <button type="button" className="secondary-btn" aria-label={`Actions for ${u.email}`} onClick={onMenu}>
          ⋯
        </button>
        {menuOpen && (
          <div
            role="menu"
            style={{
              position: "absolute",
              right: 0,
              top: "100%",
              zIndex: 5,
              minWidth: 190,
              background: "var(--voitta-bg)",
              border: "1px solid var(--voitta-border)",
              borderRadius: 6,
              boxShadow: "var(--voitta-shadow)",
              padding: 4,
            }}
          >
            {items.map((it) => (
              <button
                key={it.label}
                type="button"
                role="menuitem"
                onClick={it.run}
                style={{
                  display: "block",
                  width: "100%",
                  textAlign: "left",
                  padding: "6px 10px",
                  border: "none",
                  background: "transparent",
                  color: it.label === "Disable" ? "var(--voitta-danger, #ef4444)" : "var(--voitta-text)",
                  cursor: "pointer",
                  borderRadius: 4,
                }}
              >
                {it.label}
              </button>
            ))}
          </div>
        )}
      </div>
      {children}
    </div>
  );
}

function InviteForm({
  api,
  onDone,
}: {
  api: (path: string, body?: unknown) => Promise<Record<string, unknown>>;
  onDone: (msg: string) => void;
}) {
  const [emails, setEmails] = useState("");
  const [role, setRole] = useState<"user" | "admin">("user");
  const [pw, setPw] = useState(DEFAULT_PASSWORD);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function submit() {
    setBusy(true);
    setErr(null);
    try {
      const b = await api("/users/invite", { emails, role, password: pw || null });
      const res = (b.results as { email: string; status: string }[]) ?? [];
      onDone(res.map((x) => `${x.email} ${x.status}`).join(" · ") || "Nothing to invite.");
      if (res.some((x) => x.status === "invited")) setEmails("");
    } catch (e) {
      setErr(String((e as Error).message || e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={{ border: "1px solid var(--voitta-border)", borderRadius: 8, padding: 10, display: "grid", gap: 8 }}>
      <label className="muted" style={{ fontSize: 12 }}>
        Emails (comma or one per line)
        <textarea rows={2} value={emails} onChange={(e) => setEmails(e.currentTarget.value)} style={{ width: "100%" }} />
      </label>
      <div style={{ display: "flex", gap: 14, fontSize: 13 }}>
        <span className="muted">Role</span>
        {(["user", "admin"] as const).map((r) => (
          <label key={r}>
            <input type="radio" checked={role === r} onChange={() => setRole(r)} /> {r}
          </label>
        ))}
      </div>
      <PasswordField value={pw} onChange={setPw} api={api} />
      <div className="muted" style={{ fontSize: 12 }}>Leave the password empty for Google-only sign-in.</div>
      {err && <div className="status err">{err}</div>}
      <button type="button" className="save-btn" disabled={busy || !emails.trim()} onClick={submit}>
        {busy ? "Inviting…" : "Invite"}
      </button>
    </div>
  );
}

function PasswordField({
  value,
  onChange,
  api,
}: {
  value: string;
  onChange: (v: string) => void;
  api: (path: string, body?: unknown) => Promise<Record<string, unknown>>;
}) {
  return (
    <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
      <span className="muted" style={{ fontSize: 12 }}>Password</span>
      <input
        type="text"
        autoComplete="off"
        spellCheck={false}
        value={value}
        onChange={(e) => onChange(e.currentTarget.value)}
        style={{ flex: 1, minWidth: 0, fontFamily: "ui-monospace, Menlo, monospace" }}
      />
      <button
        type="button"
        className="secondary-btn"
        title="Generate a readable password"
        onClick={async () => onChange(String((await api("/generate-password")).password || ""))}
      >
        ⟳ generate
      </button>
    </div>
  );
}

function SetPasswordForm({
  email,
  act,
  api,
  onClose,
}: {
  email: string;
  act: (action: string, body: Record<string, unknown>, done?: string) => Promise<unknown>;
  api: (path: string, body?: unknown) => Promise<Record<string, unknown>>;
  onClose: () => void;
}) {
  const [pw, setPw] = useState("");
  return (
    <div style={{ border: "1px solid var(--voitta-border)", borderRadius: 8, padding: 10, margin: "6px 0", display: "grid", gap: 8 }}>
      <div style={{ fontSize: 13 }}>
        Set password: <b>{email}</b>
      </div>
      <PasswordField value={pw} onChange={setPw} api={api} />
      <div className="muted" style={{ fontSize: 12 }}>
        ⓘ Typed here, the page you're on can see it — generate is safer. Their other sessions end.
      </div>
      <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
        <button type="button" className="secondary-btn" onClick={onClose}>Cancel</button>
        <button
          type="button"
          className="save-btn"
          disabled={!pw}
          onClick={async () => {
            const ok = await act("password", { email, mode: "set", password: pw }, `Password set for ${email}: ${pw}`);
            if (ok) onClose();
          }}
        >
          Set
        </button>
      </div>
    </div>
  );
}

function ActivityLog({ api }: { api: (path: string, body?: unknown) => Promise<Record<string, unknown>> }) {
  const [rows, setRows] = useState<Activity[] | null>(null);
  return (
    <details
      onToggle={(e) => {
        if ((e.currentTarget as HTMLDetailsElement).open)
          api("/activity?limit=100").then((b) => setRows((b.activity as Activity[]) ?? []));
      }}
    >
      <summary style={{ cursor: "pointer", marginTop: 6 }}>Activity (sign-ins, failures, admin actions)</summary>
      {rows === null ? (
        <p className="muted">Loading…</p>
      ) : (
        <div style={{ fontSize: 12, marginTop: 6 }}>
          {rows.map((a) => (
            <div key={a.id} style={{ display: "flex", gap: 8, padding: "3px 0", opacity: a.ok ? 1 : 0.8 }}>
              <span className="muted" style={{ whiteSpace: "nowrap" }}>{ago(a.at)}</span>
              <span style={{ color: a.ok ? undefined : "var(--voitta-danger, #ef4444)" }}>
                {a.action.replace(/^(auth|user|site)\./, "")}
                {a.detail && "why" in a.detail ? ` (${String(a.detail.why)})` : ""}
              </span>
              <span style={{ wordBreak: "break-all" }}>{a.target}</span>
              {a.actor && a.actor !== a.target && <span className="muted">by {a.actor}</span>}
            </div>
          ))}
        </div>
      )}
    </details>
  );
}
