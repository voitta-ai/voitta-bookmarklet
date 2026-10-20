"""The Claude subscription brain (``claude_code``) under the eval policy (#30).

The chat path hands the engine the live tool registry, auto-approves a few of
its built-ins and keeps transcripts in the user's workspace. Eval needs the
opposite on each point, so :func:`policy` rewrites the engine options:

* The tool surface is the eval session's own (production schemas plus test
  tools), served in-process and routed to the eval dispatcher, which decides,
  traces and executes exactly as it does for the API-key providers.
* Nothing is auto-approved. A ``PreToolUse`` hook sees every call, including
  the read-only built-ins that never reach ``can_use_tool``: Voitta tools are
  allowed through to the dispatcher, engine built-ins are traced and denied
  (except ``ToolSearch``, which only loads schemas inside the engine). The
  built-ins stay visible, as in chat, so the model sees the same surface.
* The engine's config dir and cwd live under the eval session's directory,
  so transcripts never reach the chat history and go when the session closes.
  The subscription token comes from the environment, not the config dir.
* Every resumed turn forks, so a run that fails is never resumed from.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from app.eval.trace import Trace
from app.services.agent_sdk.bridge import build_dispatch_server
from app.services.agent_sdk.config import MCP_SERVER_NAME
from app.tools.registry import ToolResult

ENGINE_TOOL_REASON = "engine built-in tools do not execute in eval sessions"
_PREFIX = f"mcp__{MCP_SERVER_NAME}__"

# Built-ins that act only inside the engine, allowed with this reason. The
# engine defers MCP tool schemas behind ToolSearch, so without it no Voitta
# tool (test tools included) can be called at all.
_ENGINE_INTERNAL = {
    "ToolSearch": "loads tool schemas inside the engine; no effect outside it",
}

Dispatch = Callable[[str, dict[str, Any], str], Awaitable[ToolResult]]


def engine_dirs(session_dir: Path) -> tuple[Path, Path]:
    """The engine's config dir and cwd for one eval session."""
    root = session_dir / "engine"
    config, work = root / "config", root / "workspace"
    config.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    return config, work


def _key(name: str, args: dict[str, Any]) -> tuple[str, str]:
    return name, json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)


def policy(*, tools: Any, dispatch: Dispatch, trace: Trace, session_dir: Path,
           max_turns: int) -> Callable[[Any], Any]:
    """An ``SdkRunContext.options`` function that puts one run under the eval
    policy. ``dispatch(name, args, call_id)`` is the eval dispatcher."""
    from claude_agent_sdk import HookMatcher, PermissionResultAllow, PermissionResultDeny

    # The MCP handler gets only the arguments, not the tool-use id; the hook,
    # which runs just before it, does. The engine runs these tools one at a
    # time (none declares readOnlyHint, which is what lets it run tools in
    # parallel), so the hook for a call is the last one before its handler.
    # One slot, overwritten by every hook: a call the SDK rejects before the
    # handler (invalid arguments) leaves nothing behind for a later call.
    last: list[tuple[tuple[str, str], str]] = []

    async def _dispatch(name: str, args: dict[str, Any]) -> ToolResult:
        call_id = uuid.uuid4().hex
        if last and last[0][0] == _key(name, args):
            call_id = last.pop()[1]
        return await dispatch(name, args, call_id)

    async def _pre_tool_use(data: Any, tool_use_id: str | None, _context: Any) -> dict[str, Any]:
        name = str(data.get("tool_name") or "")
        args = data.get("tool_input") or {}
        call_id = data.get("tool_use_id") or tool_use_id or uuid.uuid4().hex
        if name.startswith(_PREFIX):
            last[:] = [(_key(name.removeprefix(_PREFIX), args), call_id)]
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                           "permissionDecision": "allow"}}
        allowed = name in _ENGINE_INTERNAL
        reason = _ENGINE_INTERNAL.get(name, ENGINE_TOOL_REASON)
        trace.emit("tool.requested", {"name": name, "args": args, "origin": "engine"},
                   call_id=call_id)
        trace.emit("policy.decision", {"name": name, "allowed": allowed, "reason": reason},
                   call_id=call_id)
        if allowed:
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                           "permissionDecision": "allow"}}
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                       "permissionDecision": "deny",
                                       "permissionDecisionReason": ENGINE_TOOL_REASON}}

    async def _can_use_tool(name: str, _input: dict, _ctx: Any) -> Any:
        # Backstop only: the hook has already decided every call.
        if name.startswith(_PREFIX) or name in _ENGINE_INTERNAL:
            return PermissionResultAllow()
        return PermissionResultDeny(message=ENGINE_TOOL_REASON)

    server, _ = build_dispatch_server(tools, _dispatch)
    config, work = engine_dirs(session_dir)

    def apply(options: Any) -> Any:
        env = dict(options.env or {})
        env["CLAUDE_CONFIG_DIR"] = str(config)
        return dataclasses.replace(
            options,
            cwd=str(work),
            env=env,
            mcp_servers={MCP_SERVER_NAME: server},
            allowed_tools=[],
            can_use_tool=_can_use_tool,
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[_pre_tool_use])]},
            fork_session=options.resume is not None,
            max_turns=max_turns,
        )

    return apply
