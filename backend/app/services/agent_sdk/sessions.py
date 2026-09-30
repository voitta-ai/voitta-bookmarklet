"""List and read Claude Agent SDK sessions for the history dropdown.

The SDK's read APIs (``list_sessions`` / ``get_session_messages`` /
``get_session_info``) resolve their storage location from
``os.environ["CLAUDE_CONFIG_DIR"]`` and have no per-call override. To list a
*specific* user's sessions in server mode we set that env var around the call.
Because the process env is global, the set/read/restore is serialised under an
``asyncio.Lock`` and the (sync, disk-bound) SDK call runs in a worker thread.

This is correct on a single box. A load-balanced, multi-instance server should
instead back the brain with an external ``SessionStore`` so listing is explicit
rather than disk-local — out of scope here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from typing import Any, Iterator

# Installed at runtime by app.installer — import defensively. The list/read
# routes guard on is_available() and try/except, so a missing SDK degrades to
# an empty session list rather than a crash.
try:
    from claude_agent_sdk import (
        get_session_info,
        get_session_messages,
        list_sessions,
    )
except ImportError:  # SDK not installed yet
    get_session_info = get_session_messages = list_sessions = None  # type: ignore

from app.services.agent_sdk.config import (
    MCP_SERVER_NAME,
    SYNTHETIC_MODEL,
    config_dir,
    workspace_dir,
)

logger = logging.getLogger(__name__)

_env_lock = asyncio.Lock()


@contextlib.contextmanager
def _config_env(path: str) -> Iterator[None]:
    prior = os.environ.get("CLAUDE_CONFIG_DIR")
    os.environ["CLAUDE_CONFIG_DIR"] = path
    try:
        yield
    finally:
        if prior is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = prior


def _info_to_dict(info: Any) -> dict[str, Any]:
    return {
        "session_id": getattr(info, "session_id", None),
        "title": getattr(info, "custom_title", None) or getattr(info, "summary", None) or "",
        "summary": getattr(info, "summary", None),
        "first_prompt": getattr(info, "first_prompt", None),
        "last_modified": getattr(info, "last_modified", None),
        "created_at": getattr(info, "created_at", None),
        "tag": getattr(info, "tag", None),
        "git_branch": getattr(info, "git_branch", None),
    }


async def list_brain_sessions(limit: int = 100) -> list[dict[str, Any]]:
    """Sessions for the current user's pinned workspace, newest-first."""
    cfg = str(config_dir())
    cwd = str(workspace_dir())

    def _call() -> list[Any]:
        with _config_env(cfg):
            return list_sessions(directory=cwd, limit=limit)

    async with _env_lock:
        infos = await asyncio.to_thread(_call)
    out = [_info_to_dict(i) for i in infos]
    out.sort(key=lambda r: r.get("last_modified") or "", reverse=True)
    return out


async def get_brain_session_info(session_id: str) -> dict[str, Any] | None:
    cfg = str(config_dir())
    cwd = str(workspace_dir())

    def _call() -> Any:
        with _config_env(cfg):
            return get_session_info(session_id, directory=cwd)

    async with _env_lock:
        info = await asyncio.to_thread(_call)
    return _info_to_dict(info) if info is not None else None


def _transcript_rows(msgs: list[Any]) -> list[dict[str, Any]]:
    """Project stored SessionMessages into display rows, in order:

    * ``{"role": "user" | "assistant", "text"}`` — prose. Consecutive text
      blocks within one message join into one row, as the live turn renders a
      contiguous run as one bubble.
    * ``{"role": "tool", "id", "name", "input", "output", "is_error"}`` — a tool
      call, emitted where the ``tool_use`` sits and filled in when its
      ``tool_result`` (carried by a later user message) arrives. Mirrors the
      live turn's tool steps: same name stripping, same input/output flattening.

    Engine-synthetic assistant messages are dropped, as they are live.
    """
    from app.services.agent_sdk.runtime import _tool_result_text, _truncate

    rows: list[dict[str, Any]] = []
    tools: dict[str, dict[str, Any]] = {}
    prefix = f"mcp__{MCP_SERVER_NAME}__"
    for sm in msgs:
        raw = getattr(sm, "message", None)
        if not isinstance(raw, dict):
            continue
        role = raw.get("role")
        if role not in ("user", "assistant") or raw.get("model") == SYNTHETIC_MODEL:
            continue
        content = raw.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
        if not isinstance(blocks, list):
            continue
        text_run: list[str] = []

        def flush() -> None:
            text = "\n".join(text_run).strip()
            text_run.clear()
            if text:
                rows.append({"role": role, "text": text})

        for b in blocks:
            if not isinstance(b, dict):
                continue
            kind = b.get("type")
            if kind == "text":
                text_run.append(str(b.get("text", "")))
            elif kind == "tool_use" and role == "assistant":
                flush()
                try:
                    tool_input = json.dumps(b.get("input"), ensure_ascii=False, default=str)
                except Exception:
                    tool_input = str(b.get("input"))
                row = {
                    "role": "tool",
                    "id": b.get("id"),
                    "name": str(b.get("name") or "tool").removeprefix(prefix),
                    "input": _truncate(tool_input),
                    "output": None,
                    "is_error": False,
                }
                rows.append(row)
                if b.get("id"):
                    tools[b["id"]] = row
            elif kind == "tool_result":
                row = tools.get(b.get("tool_use_id"))
                if row is not None:
                    row["output"] = _truncate(_tool_result_text(b.get("content")))
                    row["is_error"] = bool(b.get("is_error"))
        flush()
    # A call whose turn died before the result landed. Left empty, the UI would
    # render it as still running.
    for row in tools.values():
        if row["output"] is None:
            row["output"] = "(no result recorded — the turn was interrupted)"
            row["is_error"] = True
    return rows


async def get_brain_transcript(session_id: str, limit: int | None = None) -> list[dict[str, Any]]:
    """Display transcript (text and tool-call rows) for one session."""
    cfg = str(config_dir())
    cwd = str(workspace_dir())

    def _call() -> list[Any]:
        with _config_env(cfg):
            return get_session_messages(session_id, directory=cwd, limit=limit)

    async with _env_lock:
        msgs = await asyncio.to_thread(_call)
    return _transcript_rows(msgs)
