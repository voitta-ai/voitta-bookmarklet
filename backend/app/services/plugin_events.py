"""Plugin event log — small, per-user, per-plugin diagnostics a plugin's browser code records.

Kept on the server (not in the page) so it survives what it's meant to diagnose: a site logout
that navigates the tab away wipes everything in the page. Read back by the plugin's settings
panel and by its agent tools.

Storage: ``<user data root>/plugin_events/<plugin>.jsonl``, one event per line. Capped at
``MAX_EVENTS`` and ``MAX_AGE_S``; trimmed on write. Events are small dicts the plugin defines:
``{"t": <epoch ms>, "type": "...", ...}``. The server stamps ``rt`` (receive time, epoch ms).
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

MAX_EVENTS = 1000
MAX_AGE_S = 14 * 86_400
MAX_BATCH = 50
MAX_EVENT_BYTES = 2_000

_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_lock = threading.Lock()


class EventError(ValueError):
    pass


def _path(plugin: str) -> Path:
    from app.services.current_user import user_data_root

    if not _NAME.match(plugin or ""):
        raise EventError("bad plugin name")
    return user_data_root() / "plugin_events" / f"{plugin}.jsonl"


def _read(path: Path) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict):
            out.append(ev)
    return out


def _clean(ev: Any, now_ms: int) -> dict | None:
    if not isinstance(ev, dict):
        return None
    ev = {str(k)[:40]: v for k, v in ev.items() if isinstance(v, (str, int, float, bool)) or v is None}
    ev["type"] = str(ev.get("type") or "event")[:40]
    try:
        t = int(ev.get("t") or now_ms)
    except (TypeError, ValueError):
        t = now_ms
    ev["t"] = t if abs(now_ms - t) < 7 * 86_400_000 else now_ms  # ignore wild clocks
    ev["rt"] = now_ms
    if len(json.dumps(ev, ensure_ascii=False)) > MAX_EVENT_BYTES:
        return None
    return ev


def append(plugin: str, events: list[Any]) -> int:
    """Add events (oldest first). Returns how many were kept."""
    if not isinstance(events, list):
        raise EventError("events must be a list")
    now_ms = int(time.time() * 1000)
    clean = [e for e in (_clean(x, now_ms) for x in events[:MAX_BATCH]) if e]
    if not clean:
        return 0
    path = _path(plugin)
    with _lock:
        cutoff = now_ms - MAX_AGE_S * 1000
        kept = [e for e in _read(path) if int(e.get("rt") or 0) >= cutoff] + clean
        kept = kept[-MAX_EVENTS:]
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".jsonl.tmp")
        tmp.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in kept), encoding="utf-8")
        tmp.replace(path)
    return len(clean)


def read(plugin: str, limit: int = MAX_EVENTS) -> list[dict]:
    """Events, oldest first (at most ``limit`` of the newest)."""
    with _lock:
        evs = _read(_path(plugin))
    return evs[-max(1, min(int(limit), MAX_EVENTS)):]


def clear(plugin: str) -> None:
    with _lock:
        try:
            _path(plugin).unlink()
        except FileNotFoundError:
            pass
