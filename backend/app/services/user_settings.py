"""Backend-stored user settings (LLM API keys, provider, model, caps).

Migrated out of the browser's localStorage so settings persist across
host origins (the bookmarklet is the same widget regardless of which
site you click it on, but ``localStorage`` is partitioned per origin —
keys saved on one host would be invisible on another). The new home is
a JSON blob in the user's XDG config dir, owned by the running user
(``0600``).

Stays on the host machine. The backend already binds to ``127.0.0.1``,
so this file is no more reachable from the network than the LLM-key
state in localStorage was. Trade-off accepted: keys end up in plain
text on disk.

The blob is opaque to this module — we don't validate provider IDs,
model names, etc. Validation lives in the frontend (``lib/settings.ts``)
and is reapplied on every read there. Server-side, we only enforce
that the blob is a JSON object.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


# Sourced from ``app.config`` so the chainlit build keeps its own
# settings file (``~/.config/voitta-bookmarklet/settings.json``)
# rather than clobbering / inheriting the legacy bookmarklet's blob.
from app.config import USER_CONFIG_DIR, USER_SETTINGS_PATH


def _settings_dir() -> Path:
    """Directory holding the active settings.json.

    Server mode (a user is authenticated — the current-user contextvar is
    set by the HTTP guard / chat handlers): the user's own data folder, so
    API keys, provider, models, plugin config and Drive/Sheets OAuth are all
    per-user. Desktop / dev (no current user): the shared XDG config dir,
    unchanged. Resolved per call so it tracks the contextvar.
    """
    from app.services.current_user import get_current_email, data_root_for_email

    email = get_current_email()
    if email:
        return data_root_for_email(email)
    return USER_CONFIG_DIR


def _settings_path() -> Path:
    return _settings_dir() / "settings.json"


def mcp_debug_enabled() -> bool:
    """Whether the localhost-only MCP debugging endpoint at ``/mcp`` is
    exposed. Default **False**: the surface lets external MCP clients
    enumerate live bookmarklet sessions and eval JS in them, which is
    exactly the level of privilege we want gated behind an explicit
    user toggle. The tray-bar Settings dialog flips this; the
    ``_MCPGate`` middleware (``app.routes.mcp``) reads it on every
    request so toggling takes effect without a restart.
    """
    try:
        return bool(read().get("mcpDebugEnabled", False))
    except Exception:
        return False


def set_mcp_debug_enabled(enabled: bool) -> None:
    """Persist the MCP-debug kill switch. Used by the tray menu."""
    _set_flag("mcpDebugEnabled", enabled)


def mcp_tools_enabled() -> bool:
    """Whether ``/mcp`` also exposes the agent's tool registry (the ``vb_*``
    tools) so an external agent — Claude Code, say — can drive everything
    the in-app agent can, without an API key in the app. Default **False**,
    and separate from the debugging switch: this surface runs Python
    (``run_script``) and writes to connected services, a wider grant than
    page inspection. Read per request, like the debug switch.
    """
    try:
        return bool(read().get("mcpToolsEnabled", False))
    except Exception:
        return False


def set_mcp_tools_enabled(enabled: bool) -> None:
    """Persist the agent-tools switch. Used by the tray menu."""
    _set_flag("mcpToolsEnabled", enabled)


def mcp_chat_enabled() -> bool:
    """Whether ``/mcp`` offers ``mcp_inject_text``: an external agent posting
    messages into the in-app agent's chat. Default **False**, and separate
    from debugging — a posted message runs a full in-app turn as the user,
    on the app's model access. Read per request.
    """
    try:
        return bool(read().get("mcpChatEnabled", False))
    except Exception:
        return False


def set_mcp_chat_enabled(enabled: bool) -> None:
    """Persist the in-app-agent switch. Used by the tray menu."""
    _set_flag("mcpChatEnabled", enabled)


def _set_flag(key: str, enabled: bool) -> None:
    blob: dict[str, Any] = {}
    try:
        blob = read()
    except Exception:
        blob = {}
    blob[key] = bool(enabled)
    write(blob)


def voice_enabled() -> bool:
    """Whether the "hey voitta" voice assistant is on. Default **False**
    — listening to the mic is strictly opt-in via the tray menu's Voice
    item. Desktop mode only; server mode has no microphone."""
    try:
        return bool(read().get("voiceEnabled", False))
    except Exception:
        return False


def set_voice_enabled(enabled: bool) -> None:
    """Persist the voice-assistant toggle. Used by the tray menu."""
    blob: dict[str, Any] = {}
    try:
        blob = read()
    except Exception:
        blob = {}
    blob["voiceEnabled"] = bool(enabled)
    write(blob)


# Mic sensitivity: the CEILING for the voice pipeline's adaptive gain
# (AutoGain), not a fixed multiplier. Quiet/distant mics under-drive the
# wake spotter, VAD, and transcription so the user "has to yell"; AutoGain
# boosts toward a target level, and this caps how much boost a very quiet
# mic may get. 1.0 = off (raw audio). A normal voice is never amplified
# into clipping regardless of the ceiling.
MIC_GAIN_MIN = 1.0
MIC_GAIN_MAX = 24.0
MIC_GAIN_DEFAULT = 6.0


def mic_gain() -> float:
    """Adaptive-gain ceiling for the voice pipeline. Default 6.0."""
    try:
        g = float(read().get("voiceMicGain", MIC_GAIN_DEFAULT))
    except Exception:
        return MIC_GAIN_DEFAULT
    return max(MIC_GAIN_MIN, min(MIC_GAIN_MAX, g))


def set_mic_gain(gain: float) -> None:
    """Persist the mic-sensitivity gain (clamped)."""
    blob: dict[str, Any] = {}
    try:
        blob = read()
    except Exception:
        blob = {}
    blob["voiceMicGain"] = max(MIC_GAIN_MIN, min(MIC_GAIN_MAX, float(gain)))
    write(blob)


def read() -> dict[str, Any]:
    """Return the persisted settings dict, or ``{}`` if no file yet.

    Tolerates a missing or unreadable file (returns ``{}``); only a
    *corrupt* file (invalid JSON or non-object root) raises so the
    caller knows manual intervention is needed.
    """
    path = _settings_path()
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(
            f"settings file at {path} is not a JSON object"
        )
    return data


def write(blob: dict[str, Any]) -> None:
    """Persist ``blob`` to disk atomically with ``0600`` perms.

    Atomic via tmpfile + ``os.replace`` so a crash mid-write never
    leaves a half-written file. Mode is set on the tmpfile before the
    rename so the final file is born ``0600``, never momentarily
    world-readable.
    """
    if not isinstance(blob, dict):
        raise TypeError("settings must be a dict")
    settings_dir = _settings_dir()
    settings_path = settings_dir / "settings.json"
    settings_dir.mkdir(parents=True, exist_ok=True)
    # Lock down the dir too — same user-only intent.
    try:
        os.chmod(settings_dir, 0o700)
    except OSError:
        pass

    fd, tmp_path = tempfile.mkstemp(
        prefix=".settings.", suffix=".json.tmp", dir=str(settings_dir)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(blob, fh, ensure_ascii=False, indent=2)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, settings_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
