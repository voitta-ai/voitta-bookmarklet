"""Routines — named collections of scheduled prompts ("chimes").

A routine is armed per chat in the browser; when one of its chimes comes
due, the tab sends the chime's prompt into the chat as a user turn — unless
the chat is busy, in which case that chime is simply dropped.

The backend only stores and validates routines, and arbitrates
:func:`claim` so a chat open in several tabs fires each slot once. All
schedule math (time zones, "what's due now") runs in the browser.

Storage: ``routines.json`` beside the user's ``settings.json`` — per-user in
server mode, global (not project-scoped) like settings.

Shape::

    {"routines": [
      {"id": "r_ab12", "name": "Market hours watch", "tz": "America/New_York",
       "chimes": [
         {"id": "c_1", "mode": "every", "every_min": 30,
          "start": "09:30", "end": "16:00", "days": [0,1,2,3,4],
          "times": [], "prompt": "Check positions…"},
         {"id": "c_2", "mode": "at", "times": ["15:55"],
          "days": [0,1,2,3,4], "prompt": "Close day trades…"}]}]}

``days`` uses 0 = Monday … 6 = Sunday; empty means every day.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MIN_EVERY_MIN = 10  # floor on "every N minutes" so a typo can't hammer the model
MAX_ROUTINES = 50
MAX_CHIMES = 50
MAX_TIMES = 48
MAX_PROMPT_CHARS = 8000
MAX_NAME_CHARS = 80

_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")

_FILENAME = "routines.json"
_write_lock = threading.Lock()

def _path() -> Path:
    from app.services.user_settings import _settings_dir

    return _settings_dir() / _FILENAME

def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(4)}"

def _hhmm(value: Any, field: str) -> str:
    s = str(value or "").strip()
    if not _HHMM.match(s):
        raise ValueError(f"{field}: expected HH:MM (24h), got {value!r}")
    return s

def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)

def _validate_chime(raw: Any, idx: int) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"chime {idx + 1}: must be an object")
    where = f"chime {idx + 1}"
    cid = str(raw.get("id") or "")
    if not _ID.match(cid):
        cid = _new_id("c")

    prompt = str(raw.get("prompt") or "").strip()
    if not prompt:
        raise ValueError(f"{where}: prompt is empty")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ValueError(f"{where}: prompt longer than {MAX_PROMPT_CHARS} chars")

    days_raw = raw.get("days") or []
    if not isinstance(days_raw, list):
        raise ValueError(f"{where}: days must be a list")
    days = sorted({int(d) for d in days_raw})
    if any(d < 0 or d > 6 for d in days):
        raise ValueError(f"{where}: days must be 0 (Mon) … 6 (Sun)")

    mode = raw.get("mode")
    out: dict[str, Any] = {"id": cid, "mode": mode, "days": days, "prompt": prompt}
    if mode == "every":
        try:
            every = int(raw.get("every_min"))
        except (TypeError, ValueError):
            raise ValueError(f"{where}: every_min must be a number")
        if every < MIN_EVERY_MIN:
            raise ValueError(f"{where}: minimum interval is {MIN_EVERY_MIN} minutes")
        if every > 24 * 60:
            raise ValueError(f"{where}: interval longer than a day")
        start = _hhmm(raw.get("start"), f"{where} start")
        end = _hhmm(raw.get("end"), f"{where} end")
        if _minutes(end) < _minutes(start):
            raise ValueError(f"{where}: end is before start (overnight windows aren't supported)")
        out.update(every_min=every, start=start, end=end, times=[])
    elif mode == "at":
        times_raw = raw.get("times") or []
        if not isinstance(times_raw, list) or not times_raw:
            raise ValueError(f"{where}: add at least one time")
        if len(times_raw) > MAX_TIMES:
            raise ValueError(f"{where}: at most {MAX_TIMES} times")
        times = sorted({_hhmm(t, f"{where} time") for t in times_raw}, key=_minutes)
        out.update(times=times)
    else:
        raise ValueError(f"{where}: mode must be 'every' or 'at'")
    return out

def validate_routine(raw: Any) -> dict:
    """Normalise one routine; raises ``ValueError`` with a user-facing message."""
    if not isinstance(raw, dict):
        raise ValueError("routine must be an object")
    name = str(raw.get("name") or "").strip()
    if not name:
        raise ValueError("routine name is empty")
    if len(name) > MAX_NAME_CHARS:
        raise ValueError(f"routine name longer than {MAX_NAME_CHARS} chars")
    rid = str(raw.get("id") or "")
    if not _ID.match(rid):
        rid = _new_id("r")
    tz = str(raw.get("tz") or "").strip()
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"{name}: unknown time zone {tz!r}")
    chimes_raw = raw.get("chimes") or []
    if not isinstance(chimes_raw, list):
        raise ValueError(f"{name}: chimes must be a list")
    if len(chimes_raw) > MAX_CHIMES:
        raise ValueError(f"{name}: at most {MAX_CHIMES} chimes")
    try:
        chimes = [_validate_chime(c, i) for i, c in enumerate(chimes_raw)]
    except ValueError as exc:
        raise ValueError(f"{name}: {exc}") from None
    return {"id": rid, "name": name, "tz": tz, "chimes": chimes}

def list_routines() -> list[dict]:
    path = _path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError):
        return []
    items = data.get("routines") if isinstance(data, dict) else None
    return items if isinstance(items, list) else []

def save_routines(raw: Any) -> list[dict]:
    """Validate and atomically replace the whole collection."""
    if not isinstance(raw, list):
        raise ValueError("routines must be a list")
    if len(raw) > MAX_ROUTINES:
        raise ValueError(f"at most {MAX_ROUTINES} routines")
    routines = [validate_routine(r) for r in raw]
    seen: set[str] = set()
    for r in routines:  # duplicate ids (e.g. a pasted copy) get fresh ones
        while r["id"] in seen:
            r["id"] = _new_id("r")
        seen.add(r["id"])

    path = _path()
    with _write_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".routines.", suffix=".json.tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"routines": routines}, fh, ensure_ascii=False, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    return routines

# ── cross-tab claim ────────────────────────────────────────────────────────
# One chat can be open (and armed) in several tabs. Before firing a slot a
# tab claims (owner, chat, routine, chime, slot); only the first claim wins.
# In-memory is enough: slots are minutes apart and a restart mid-slot at
# worst lets one duplicate through.

_CLAIM_TTL_S = 15 * 60
_claims: dict[tuple[str, str, str, str, int], float] = {}
_claims_lock = threading.Lock()

def claim(chat_key: str, routine_id: str, chime_id: str, slot_ms: int) -> bool:
    owner = str(_path().parent)
    key = (owner, chat_key, routine_id, chime_id, int(slot_ms))
    now = time.monotonic()
    with _claims_lock:
        for k in [k for k, exp in _claims.items() if exp < now]:
            del _claims[k]
        if key in _claims:
            return False
        _claims[key] = now + _CLAIM_TTL_S
        return True
