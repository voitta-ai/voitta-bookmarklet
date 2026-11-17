"""hg keys + activity — ``USER_DATA_ROOT/hg.sqlite``, separate from the account database.

A key (``hgk_…``) is the only identity on hg.voitta.ai. Only its SHA-256 is stored; the plain key
lives in the user's browser (StockTrak site storage) and in their API client. One key per browser:
the panel creates it on first use and can regenerate it (the old one stops working at once).

Per key: ``trading`` (allow placing/changing orders through the API; on by default — it's a game)
and ``keepalive`` (the panel pings StockTrak so the session survives an idle API; on by default).
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

KEY_PREFIX = "hgk_"
MINT_PER_IP_PER_HOUR = 30
ACTIVITY_KEEP = 200

_SCHEMA = """
CREATE TABLE IF NOT EXISTS keys(
  id INTEGER PRIMARY KEY,
  prefix TEXT NOT NULL,
  key_hash TEXT NOT NULL UNIQUE,
  created_at REAL NOT NULL,
  created_ip TEXT,
  last_used REAL,
  revoked_at REAL,
  trading INTEGER NOT NULL DEFAULT 1,
  keepalive INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS activity(
  id INTEGER PRIMARY KEY,
  key_id INTEGER NOT NULL,
  at REAL NOT NULL,
  via TEXT NOT NULL,
  op TEXT NOT NULL,
  ok INTEGER NOT NULL,
  code TEXT,
  ms INTEGER
);
CREATE INDEX IF NOT EXISTS activity_key ON activity(key_id, id);
"""


class HgError(ValueError):
    pass


def db_path() -> Path:
    from app import config

    return config.USER_DATA_ROOT / "hg.sqlite"


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
    conn.executescript(_SCHEMA)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
    _conn, _conn_path = conn, path
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
        _mints.clear()


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def public(row: dict) -> dict:
    return {"id": row["id"], "prefix": row["prefix"], "created_at": row["created_at"],
            "last_used": row["last_used"], "trading": bool(row["trading"]), "keepalive": bool(row["keepalive"])}


# ── minting (no sign-in: limited per IP so the open endpoint can't flood the table) ─

_mints: dict[str, list[float]] = {}


def _check_mint_rate(ip: str | None) -> None:
    now = time.time()
    hits = [t for t in _mints.get(ip or "?", []) if now - t < 3600]
    if len(hits) >= MINT_PER_IP_PER_HOUR:
        raise HgError("Too many keys created from this address. Try again later.")
    hits.append(now)
    _mints[ip or "?"] = hits


def mint(ip: str | None = None) -> tuple[str, dict]:
    """Create a key. Returns ``(plain key — shown once, public info)``."""
    _check_mint_rate(ip)
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    cur = _x("INSERT INTO keys(prefix, key_hash, created_at, created_ip) VALUES (?,?,?,?)",
             (key[:12], _hash(key), time.time(), ip))
    return key, public(_one("SELECT * FROM keys WHERE id=?", (cur.lastrowid,)))


_SEEN_EVERY_S = 60
_seen: dict[int, float] = {}


def lookup(key: str | None) -> dict | None:
    """The live key row for a plain key, or None (wrong, revoked, not an hg key)."""
    if not key or not key.startswith(KEY_PREFIX) or len(key) > 200:
        return None
    row = _one("SELECT * FROM keys WHERE key_hash=? AND revoked_at IS NULL", (_hash(key),))
    if row is None:
        return None
    now = time.time()
    if now - _seen.get(row["id"], 0) > _SEEN_EVERY_S:
        _seen[row["id"]] = now
        _x("UPDATE keys SET last_used=? WHERE id=?", (now, row["id"]))
    return row


def get(key_id: int) -> dict | None:
    return _one("SELECT * FROM keys WHERE id=? AND revoked_at IS NULL", (key_id,))


def regenerate(key: str, ip: str | None = None) -> tuple[str, dict]:
    """Swap a key for a new one with the same settings; the old one stops working now."""
    row = lookup(key)
    if row is None:
        raise HgError("Unknown or revoked key.")
    new = KEY_PREFIX + secrets.token_urlsafe(32)
    with _lock:
        _x("UPDATE keys SET revoked_at=? WHERE id=?", (time.time(), row["id"]))
        cur = _x("INSERT INTO keys(prefix, key_hash, created_at, created_ip, trading, keepalive) "
                 "VALUES (?,?,?,?,?,?)",
                 (new[:12], _hash(new), time.time(), ip, row["trading"], row["keepalive"]))
    return new, public(_one("SELECT * FROM keys WHERE id=?", (cur.lastrowid,)))


def update_settings(key_id: int, *, trading: bool | None = None, keepalive: bool | None = None) -> dict:
    if trading is not None:
        _x("UPDATE keys SET trading=? WHERE id=?", (1 if trading else 0, key_id))
    if keepalive is not None:
        _x("UPDATE keys SET keepalive=? WHERE id=?", (1 if keepalive else 0, key_id))
    row = get(key_id)
    if row is None:
        raise HgError("Unknown or revoked key.")
    return public(row)


# ── activity (what the API did for this key: shown in the panel) ──────────


def log(key_id: int, via: str, op: str, ok: bool, code: str | None = None, ms: int | None = None) -> None:
    with _lock:
        _x("INSERT INTO activity(key_id, at, via, op, ok, code, ms) VALUES (?,?,?,?,?,?,?)",
           (key_id, time.time(), via[:10], op[:60], 1 if ok else 0, (code or "")[:40] or None, ms))
        _x("DELETE FROM activity WHERE key_id=? AND id NOT IN "
           "(SELECT id FROM activity WHERE key_id=? ORDER BY id DESC LIMIT ?)", (key_id, key_id, ACTIVITY_KEEP))


def activity(key_id: int, limit: int = 50) -> list[dict[str, Any]]:
    rows = _all("SELECT at, via, op, ok, code, ms FROM activity WHERE key_id=? ORDER BY id DESC LIMIT ?",
                (key_id, max(1, min(int(limit), ACTIVITY_KEEP))))
    for r in rows:
        r["ok"] = bool(r["ok"])
    return rows
