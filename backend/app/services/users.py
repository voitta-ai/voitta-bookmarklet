"""Users, sessions, approved sites and activity — ``USER_DATA_ROOT/users.sqlite``.

Server mode (``login_auth.is_enabled()``):

* **Invite only.** A person gets in only if their email is in ``users`` (or listed in
  ``VOITTA_OWNERS``, which are created on first sign-in). The account *is* the email: the same
  user can sign in with Google or with a password, whichever they have.
* **Owners** come from ``VOITTA_OWNERS``; they are always admins and can't be demoted or disabled.
* **Disable, never delete.** A disabled user can't sign in, every session ends at once, and their
  data stays on disk. ``enable`` brings them back.
* **Sessions.** Every sign-in creates a row here; its id (``sid``) rides in the Chainlit JWT's
  metadata. The HTTP guard and every chat entry point re-check it, so revoking a session or
  disabling a user cuts them off at their next request or message.

Both modes:

* **Approved sites.** The origins a user has allowed the widget on (desktop: one list, owner
  ``"local"``). The guard refuses signed-in calls from any other site. See
  :mod:`app.services.site_check`.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from app.services import passwords

MAX_FAILS = 8
LOCK_S = 15 * 60
LOCAL_OWNER = "local"  # approved-sites owner key on the desktop app (no sign-in)
_SEEN_EVERY_S = 60     # throttle last_seen writes

_EMAIL = re.compile(r"^[^@\s,;<>]{1,64}@[^@\s,;<>]{1,190}\.[^@\s,;<>]{1,60}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY,
  email TEXT NOT NULL UNIQUE,
  role TEXT NOT NULL DEFAULT 'user',
  password_hash TEXT,
  google_sub TEXT UNIQUE,
  created_at REAL NOT NULL,
  created_by TEXT,
  disabled_at REAL,
  disabled_by TEXT,
  last_seen REAL,
  last_method TEXT,
  failed_count INTEGER NOT NULL DEFAULT 0,
  locked_until REAL
);
CREATE TABLE IF NOT EXISTS sessions(
  sid TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  method TEXT NOT NULL,
  created_at REAL NOT NULL,
  last_seen REAL NOT NULL,
  revoked_at REAL,
  ip TEXT
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS sites(
  owner TEXT NOT NULL,
  origin TEXT NOT NULL,
  added_at REAL NOT NULL,
  PRIMARY KEY(owner, origin)
);
CREATE TABLE IF NOT EXISTS activity(
  id INTEGER PRIMARY KEY,
  at REAL NOT NULL,
  actor TEXT,
  action TEXT NOT NULL,
  target TEXT,
  ok INTEGER NOT NULL DEFAULT 1,
  detail TEXT,
  ip TEXT
);
CREATE INDEX IF NOT EXISTS activity_at ON activity(at);
"""


class UserError(ValueError):
    """A refused admin/user action, with a message fit to show."""


# ── connection ───────────────────────────────────────────────────────────


def db_path() -> Path:
    from app import config

    return config.USER_DATA_ROOT / "users.sqlite"


_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_conn_path: Path | None = None


def _db() -> sqlite3.Connection:
    global _conn, _conn_path
    path = db_path()
    if _conn is not None and _conn_path == path:
        return _conn
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
    _conn, _conn_path = conn, path
    _site_cache.clear()
    return conn


def _x(sql: str, args: tuple = ()) -> sqlite3.Cursor:
    with _lock:
        return _db().execute(sql, args)


def _one(sql: str, args: tuple = ()) -> dict | None:
    with _lock:
        row = _db().execute(sql, args).fetchone()
    return dict(row) if row else None


def _all(sql: str, args: tuple = ()) -> list[dict]:
    with _lock:
        return [dict(r) for r in _db().execute(sql, args).fetchall()]


def reset_for_tests() -> None:
    global _conn, _conn_path
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = _conn_path = None
        _site_cache.clear()
        _seen.clear()


# ── emails, owners, roles ────────────────────────────────────────────────


def normalize_email(raw: Any) -> str | None:
    e = str(raw or "").strip().lower()
    return e if _EMAIL.match(e) else None


def owners() -> set[str]:
    raw = os.environ.get("VOITTA_OWNERS", "")
    return {e for e in (normalize_email(p) for p in re.split(r"[,\s;]+", raw)) if e}


def role_of(u: dict) -> str:
    return "owner" if u["email"] in owners() else (u.get("role") or "user")


def is_admin(u: dict | None) -> bool:
    return bool(u) and role_of(u) in ("owner", "admin")


def get(email: str | None) -> dict | None:
    e = normalize_email(email)
    return _one("SELECT * FROM users WHERE email=?", (e,)) if e else None


def public(u: dict, now: float | None = None) -> dict:
    """What the admin UI may see about a user (never the hash)."""
    now = time.time() if now is None else now
    locked = (u.get("locked_until") or 0) > now
    return {
        "email": u["email"],
        "role": role_of(u),
        "has_password": bool(u.get("password_hash")),
        "has_google": bool(u.get("google_sub")),
        "disabled": u.get("disabled_at") is not None,
        "disabled_at": u.get("disabled_at"),
        "disabled_by": u.get("disabled_by"),
        "locked_minutes": max(1, round((u["locked_until"] - now) / 60)) if locked else 0,
        "last_seen": u.get("last_seen"),
        "last_method": u.get("last_method"),
        "created_at": u.get("created_at"),
        "created_by": u.get("created_by"),
    }


def list_users() -> list[dict]:
    """Active users first (owners, then admins, then users, each by email); disabled ones last."""
    rank = {"owner": 0, "admin": 1, "user": 2}
    rows = _all("SELECT * FROM users")
    rows.sort(key=lambda u: (u.get("disabled_at") is not None, rank.get(role_of(u), 3), u["email"]))
    return [public(u) for u in rows]


# ── activity log ─────────────────────────────────────────────────────────


def log(action: str, *, actor: str | None = None, target: str | None = None, ok: bool = True,
        detail: dict | None = None, ip: str | None = None) -> None:
    _x("INSERT INTO activity(at, actor, action, target, ok, detail, ip) VALUES (?,?,?,?,?,?,?)",
       (time.time(), actor, action, target, 1 if ok else 0,
        json.dumps(detail, ensure_ascii=False) if detail else None, ip))


def activity(limit: int = 200) -> list[dict]:
    rows = _all("SELECT * FROM activity ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 1000)),))
    for r in rows:
        r["ok"] = bool(r["ok"])
        r["detail"] = json.loads(r["detail"]) if r["detail"] else None
    return rows


# ── admin actions ────────────────────────────────────────────────────────


def _need(email: str) -> dict:
    u = get(email)
    if u is None:
        raise UserError(f"No user {email}.")
    return u


def _not_owner(u: dict, what: str) -> None:
    if u["email"] in owners():
        raise UserError(f"{u['email']} is an owner (VOITTA_OWNERS) and can't be {what}.")


def invite(emails: list[str] | str, *, role: str = "user", password: str | None = None,
           actor: str | None = None, ip: str | None = None) -> list[dict]:
    """Add people. Returns one ``{email, status}`` per input: invited | exists | invalid."""
    if role not in ("user", "admin"):
        raise UserError("Role must be user or admin.")
    if password is not None and passwords.problem(password):
        raise UserError(passwords.problem(password))
    raw = re.split(r"[,\s;]+", emails) if isinstance(emails, str) else list(emails)
    out: list[dict] = []
    seen: set[str] = set()
    pw_hash = passwords.hash_password(password) if password else None
    for item in raw:
        if not str(item).strip():
            continue
        e = normalize_email(item)
        if e is None:
            out.append({"email": str(item).strip()[:254], "status": "invalid"})
            continue
        if e in seen:
            continue
        seen.add(e)
        if get(e) is not None:
            out.append({"email": e, "status": "exists"})
            continue
        _x("INSERT INTO users(email, role, password_hash, created_at, created_by) VALUES (?,?,?,?,?)",
           (e, role, pw_hash, time.time(), actor))
        log("user.invite", actor=actor, target=e, detail={"role": role, "password": bool(pw_hash)}, ip=ip)
        out.append({"email": e, "status": "invited"})
    return out


def set_password(email: str, password: str | None, *, actor: str | None = None,
                 keep_sid: str | None = None, ip: str | None = None) -> None:
    """Set (or with None, remove) a password. Ends the user's other sessions."""
    u = _need(email)
    if password is not None and passwords.problem(password):
        raise UserError(passwords.problem(password))
    # Removing the last password is allowed: they can still use Google if the email is one.
    _x("UPDATE users SET password_hash=?, failed_count=0, locked_until=NULL WHERE id=?",
       (passwords.hash_password(password) if password else None, u["id"]))
    revoke_all(u["email"], keep_sid=keep_sid)
    log("user.password_set" if password else "user.password_removed", actor=actor, target=u["email"], ip=ip)


def set_role(email: str, role: str, *, actor: str | None = None, ip: str | None = None) -> None:
    if role not in ("user", "admin"):
        raise UserError("Role must be user or admin.")
    u = _need(email)
    _not_owner(u, "changed")
    _x("UPDATE users SET role=? WHERE id=?", (role, u["id"]))
    log("user.role", actor=actor, target=u["email"], detail={"role": role}, ip=ip)


def disable(email: str, *, actor: str | None = None, ip: str | None = None) -> None:
    u = _need(email)
    _not_owner(u, "disabled")
    if actor and normalize_email(actor) == u["email"]:
        raise UserError("You can't disable yourself.")
    _x("UPDATE users SET disabled_at=?, disabled_by=? WHERE id=?", (time.time(), actor, u["id"]))
    revoke_all(u["email"])
    log("user.disable", actor=actor, target=u["email"], ip=ip)


def enable(email: str, *, actor: str | None = None, ip: str | None = None) -> None:
    u = _need(email)
    _x("UPDATE users SET disabled_at=NULL, disabled_by=NULL, failed_count=0, locked_until=NULL WHERE id=?",
       (u["id"],))
    log("user.enable", actor=actor, target=u["email"], ip=ip)


def unlock(email: str, *, actor: str | None = None, ip: str | None = None) -> None:
    u = _need(email)
    _x("UPDATE users SET failed_count=0, locked_until=NULL WHERE id=?", (u["id"],))
    log("user.unlock", actor=actor, target=u["email"], ip=ip)


# ── sign-in ──────────────────────────────────────────────────────────────


def admit_google(email: str, sub: str, *, ip: str | None = None) -> tuple[dict | None, str]:
    """Find the user for verified Google claims. Returns ``(user, "")`` or ``(None, why)``.

    why: ``not_invited`` | ``disabled`` | ``mismatch`` (another Google account already
    signed in with this email)."""
    e = normalize_email(email)
    if e is None or not sub:
        return None, "not_invited"
    u = _one("SELECT * FROM users WHERE google_sub=?", (sub,))
    if u is not None and u["email"] != e:
        # The Google account now reports a different email: treat the email as the account.
        u = None
    if u is None:
        u = get(e)
        if u is not None and u.get("google_sub") and u["google_sub"] != sub:
            log("auth.google", target=e, ok=False, detail={"why": "mismatch"}, ip=ip)
            return None, "mismatch"
    if u is None:
        if e not in owners():
            log("auth.google", target=e, ok=False, detail={"why": "not_invited"}, ip=ip)
            return None, "not_invited"
        _x("INSERT INTO users(email, role, created_at, created_by) VALUES (?,?,?,?)",
           (e, "admin", time.time(), "VOITTA_OWNERS"))
        u = get(e)
    if u["disabled_at"] is not None:
        log("auth.google", target=e, ok=False, detail={"why": "disabled"}, ip=ip)
        return None, "disabled"
    if not u.get("google_sub"):
        try:
            _x("UPDATE users SET google_sub=? WHERE id=?", (sub, u["id"]))
        except sqlite3.IntegrityError:  # sub bound to another (old) email row
            _x("UPDATE users SET google_sub=NULL WHERE google_sub=?", (sub,))
            _x("UPDATE users SET google_sub=? WHERE id=?", (sub, u["id"]))
    return get(e), ""


def password_login(email: str, password: str, *, ip: str | None = None) -> tuple[dict | None, str]:
    """Email + password. Returns ``(user, "")`` or ``(None, why)``; why: ``wrong`` | ``locked`` |
    ``disabled``. Unknown email and wrong password are indistinguishable (same answer, same work)."""
    e = normalize_email(email)
    pw = str(password or "")[: passwords.MAX_LEN]
    u = get(e) if e else None
    now = time.time()
    if u is not None and (u.get("locked_until") or 0) > now:
        passwords.verify(pw, None)  # same work as a real check
        log("auth.password", target=e, ok=False, detail={"why": "locked"}, ip=ip)
        return None, "locked"
    if not passwords.verify(pw, u.get("password_hash") if u else None):
        if u is not None:
            fails = (u.get("failed_count") or 0) + 1
            locked = now + LOCK_S if fails >= MAX_FAILS else None
            _x("UPDATE users SET failed_count=?, locked_until=? WHERE id=?",
               (0 if locked else fails, locked, u["id"]))
            log("auth.password", target=e, ok=False, detail={"why": "locked_now" if locked else "wrong"}, ip=ip)
        else:
            log("auth.password", target=(e or str(email or "?"))[:254], ok=False, detail={"why": "wrong"}, ip=ip)
        return None, "wrong"
    if u["disabled_at"] is not None:
        log("auth.password", target=e, ok=False, detail={"why": "disabled"}, ip=ip)
        return None, "disabled"
    _x("UPDATE users SET failed_count=0, locked_until=NULL WHERE id=?", (u["id"],))
    return get(e), ""


def lock_minutes(email: str) -> int:
    u = get(email)
    left = ((u or {}).get("locked_until") or 0) - time.time()
    return max(1, round(left / 60)) if left > 0 else 0


# ── sessions ─────────────────────────────────────────────────────────────

_seen: dict[str, float] = {}


def new_session(u: dict, method: str, *, ip: str | None = None) -> str:
    sid = secrets.token_urlsafe(18)
    now = time.time()
    _x("INSERT INTO sessions(sid, user_id, method, created_at, last_seen, ip) VALUES (?,?,?,?,?,?)",
       (sid, u["id"], method, now, now, ip))
    _x("UPDATE users SET last_seen=?, last_method=? WHERE id=?", (now, method, u["id"]))
    log(f"auth.{method}", target=u["email"], ip=ip)
    return sid


def session_ok(email: str | None, sid: str | None) -> bool:
    """Is ``sid`` a live session of an active ``email``? Touches last_seen (throttled)."""
    if not email or not sid:
        return False
    row = _one(
        "SELECT s.revoked_at, u.disabled_at, u.email, u.id FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.sid=?", (sid,))
    if row is None or row["revoked_at"] is not None or row["disabled_at"] is not None:
        return False
    if row["email"] != normalize_email(email):
        return False
    now = time.time()
    if now - _seen.get(sid, 0) > _SEEN_EVERY_S:
        _seen[sid] = now
        _x("UPDATE sessions SET last_seen=? WHERE sid=?", (now, sid))
        _x("UPDATE users SET last_seen=? WHERE id=?", (now, row["id"]))
    return True


def revoke(sid: str | None) -> None:
    if sid:
        _x("UPDATE sessions SET revoked_at=? WHERE sid=? AND revoked_at IS NULL", (time.time(), sid))


def revoke_all(email: str, *, keep_sid: str | None = None, actor: str | None = None,
               ip: str | None = None) -> int:
    u = get(email)
    if u is None:
        return 0
    cur = _x("UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL AND sid IS NOT ?",
             (time.time(), u["id"], keep_sid))
    if actor:
        log("user.sign_out_everywhere", actor=actor, target=u["email"], ip=ip)
    return cur.rowcount


# ── approved sites ───────────────────────────────────────────────────────

_site_cache: dict[str, frozenset[str]] = {}


def normalize_origin(raw: Any) -> str | None:
    """``https://App.Example.com:443/x`` → ``https://app.example.com``; None if not http(s)."""
    s = str(raw or "").strip()
    if not s or s == "null":
        return None
    try:
        p = urlsplit(s)
    except ValueError:
        return None
    if p.scheme not in ("http", "https") or not p.hostname:
        return None
    host = p.hostname.lower()
    if ":" in host:  # IPv6
        host = f"[{host}]"
    try:
        port = p.port
    except ValueError:
        return None
    if port and not ((p.scheme == "http" and port == 80) or (p.scheme == "https" and port == 443)):
        return f"{p.scheme}://{host}:{port}"
    return f"{p.scheme}://{host}"


def sites(owner: str) -> list[dict]:
    return _all("SELECT origin, added_at FROM sites WHERE owner=? ORDER BY origin", (owner,))


def site_approved(owner: str, origin: str) -> bool:
    cached = _site_cache.get(owner)
    if cached is None:
        cached = frozenset(r["origin"] for r in sites(owner))
        _site_cache[owner] = cached
    return origin in cached


def approve_site(owner: str, origin: str, *, ip: str | None = None) -> str:
    o = normalize_origin(origin)
    if o is None:
        raise UserError("Not a web address.")
    _x("INSERT OR IGNORE INTO sites(owner, origin, added_at) VALUES (?,?,?)", (owner, o, time.time()))
    _site_cache.pop(owner, None)
    log("site.approve", actor=owner, target=o, ip=ip)
    return o


def remove_site(owner: str, origin: str, *, ip: str | None = None) -> None:
    o = normalize_origin(origin) or str(origin)
    _x("DELETE FROM sites WHERE owner=? AND origin=?", (owner, o))
    _site_cache.pop(owner, None)
    log("site.remove", actor=owner, target=o, ip=ip)
