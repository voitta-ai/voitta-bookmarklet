"""Drive one Claude Agent SDK turn and map its events to Chainlit primitives.

Each user turn is a single ``query()`` call against the Claude Code engine.
Multi-turn continuity is ``resume=<session_id>`` (continue-only — no fork),
which keeps the engine's session id stable so the history dropdown can list
and reopen it. The new/continued session id is captured from the terminal
``ResultMessage`` and returned to the caller, which stamps it on the session
and thread for the next turn.

Tools are the registry suite, bridged in-process (see :mod:`.bridge`), plus a
small allowlist of engine built-ins — ``Bash``, ``Read``, ``WebSearch`` and
``WebFetch`` (see ``_ALLOWED_ENGINE_TOOLS``). The engine's mutating native tools
(``Write``/``Edit``/``NotebookEdit``/…) are denied by ``can_use_tool``, so the
Voitta tools stay the primary surface for anything that changes state.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import chainlit as cl
from chainlit.context import context_var as cl_context_var, get_context as cl_get_context

# The Claude Agent SDK is installed at runtime by app.installer (like the other
# heavy LLM deps), so it may be absent at module-import time on a fresh launch.
# Import defensively: a missing SDK must not break app boot — the names below
# are only dereferenced inside run_agent_sdk_turn, which is gated behind
# is_available() and guards on ``query is None`` first.
try:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        PermissionResultAllow,
        PermissionResultDeny,
        ResultMessage,
        StreamEvent,
        SystemMessage,
        TextBlock,
        ThinkingBlock,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
        query,
    )
    from claude_agent_sdk import CLINotFoundError  # type: ignore
except ImportError:  # SDK not installed yet
    AssistantMessage = ClaudeAgentOptions = PermissionResultAllow = None  # type: ignore
    PermissionResultDeny = ResultMessage = SystemMessage = TextBlock = None  # type: ignore
    ThinkingBlock = ToolResultBlock = ToolUseBlock = UserMessage = query = None  # type: ignore
    StreamEvent = None  # type: ignore

    class CLINotFoundError(Exception):  # type: ignore
        """Placeholder so the except clause is valid when the SDK is absent."""

from app.services.agent_sdk.bridge import build_tool_server
from app.services.agent_sdk.config import (
    DEFAULT_MODEL,
    MCP_SERVER_NAME,
    SYNTHETIC_MODEL,
    engine_cli_path,
    subprocess_env,
    workspace_dir,
)
from app.services.agent_sdk.errors import AgentSdkAuthError, AgentSdkError, AgentSdkUnavailable
from app.tools.registry import ToolCtx

logger = logging.getLogger(__name__)

_AUTH_HINTS = (
    "invalid api key",
    "authentication",
    "unauthorized",
    "not logged in",
    "log in",
    "login",
    "oauth",
    "credit balance",
    "please run /login",
    "setup-token",
    "expired",
)


@dataclass
class TurnResult:
    session_id: str | None
    is_error: bool = False


# Optional wall-clock ceiling for one brain turn, in seconds. Off by default:
# real work (building a report, reading a large profile) routinely runs past
# any fixed limit, and the user ends a turn with Stop. Set
# VOITTA_BRAIN_TURN_TIMEOUT_S to impose one; on expiry the SDK generator is
# closed (terminating the engine subprocess) and the user gets a clean notice.
try:
    _TURN_TIMEOUT_S: float | None = float(os.environ.get("VOITTA_BRAIN_TURN_TIMEOUT_S") or 0) or None
except ValueError:
    _TURN_TIMEOUT_S = None


def _truncate(text: str, limit: int = 32_000) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n…[truncated: {len(text)} bytes]"


def _tool_result_text(content: Any) -> str:
    """Flatten a ToolResultBlock.content (str | list[block]) to display text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for blk in content:
            if isinstance(blk, dict):
                if blk.get("type") == "text":
                    parts.append(str(blk.get("text", "")))
                elif blk.get("type") == "image":
                    parts.append("[image]")
                else:
                    parts.append(str(blk))
            else:
                parts.append(str(blk))
        return "\n".join(parts)
    return str(content)


def _is_auth_failure(msg: ResultMessage) -> bool:
    if getattr(msg, "api_error_status", None) in (401, 403):
        return True
    blob = " ".join(
        str(x).lower()
        for x in (
            getattr(msg, "subtype", None),
            getattr(msg, "result", None),
            getattr(msg, "errors", None),
        )
        if x
    )
    return any(h in blob for h in _AUTH_HINTS)


async def user_prompt_stream(
    text: str,
    image_blocks: list[dict[str, Any]] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """One-shot streaming-input prompt.

    Streaming-input mode (an ``AsyncIterable`` prompt) is required whenever a
    ``can_use_tool`` callback is set — the SDK rejects a plain string. We yield
    exactly one user message and finish, which closes the input stream so the
    engine completes the turn.

    ``image_blocks`` (Anthropic ``{"type": "image", "source": …}`` dicts) are
    sent inline in the same user message — identical to the API-key brains'
    flow, so the model sees attached images immediately, no tool round-trip.
    """
    if image_blocks:
        content: str | list[dict[str, Any]] = [
            *([{"type": "text", "text": text}] if text else []),
            *image_blocks,
        ]
    else:
        content = text
    yield {"type": "user", "message": {"role": "user", "content": content}}


# Engine built-in tools the brain may use *alongside* the bridged Voitta suite.
# Bash is enabled deliberately: the engine reaches for it naturally, and letting
# it run a command in its pinned per-user workspace cwd is better UX than a hard
# refusal. Read is enabled because it is the engine's only way to VIEW images —
# chat attachments are persisted to the project's uploads tree and handed over
# as file paths (see chainlit_app._persist_attachments_for_engine); with Bash
# already allowed, Read grants no filesystem access Bash didn't have.
# WebSearch and WebFetch are enabled because the engine reaches for them
# constantly — they are how it checks current facts and reads a page the user
# pasted — and the alternative is the model being told mid-turn that a tool it
# can plainly see is "not available in this assistant". Note WebFetch feeds
# fetched page text to the model, so a hostile page is untrusted input; the
# engine's own prompting treats it that way. The remaining native tools
# (Write/Edit/NotebookEdit/…) stay denied, so the Voitta MCP tools remain the
# primary surface for anything that mutates state.
#
# Everything listed here is spliced into ``allowed_tools``, which AUTO-APPROVES
# it: the SDK never consults ``can_use_tool`` for a tool allowed outright (it
# warns about this — CanUseToolShadowedWarning). So this tuple is the real
# grant, and the matching branch in ``can_use_tool`` is belt-and-braces for
# callers that narrow ``allowed_tools``.
_ALLOWED_ENGINE_TOOLS: tuple[str, ...] = ("Bash", "Read", "WebSearch", "WebFetch")

# Engine tools withheld STRUCTURALLY, via ``disallowed_tools``. This is the
# boundary; the ``can_use_tool`` denial below is not one. Two ways it leaks:
#
#   * ``Agent``/``Workflow`` spawn subagents whose tool calls never reach
#     ``can_use_tool`` at all. Verified live: with only the callback denying
#     ``Write``, a subagent wrote a file to disk unobserved. ``disallowed_tools``
#     propagates into subagents; the callback does not.
#   * Anything in ``allowed_tools`` is auto-approved and skips the callback.
#
# So: mutating filesystem tools, ``Artifact`` (publishes to the web), and the
# whole fan-out surface. Blocking fan-out is also what makes "only the main
# agent can ask the user a question" a guarantee rather than a hope — the
# MCP tool handler receives no caller identity, so the only way to be sure no
# subagent asks is for no subagent to exist. (When ``Agent`` alone was blocked
# the model reached for ``Workflow`` instead, hence both.) ``Skill`` is left
# available deliberately — some skills run in subagents, but it carries too
# much capability to drop wholesale; revisit if it becomes an escape.
_DISALLOWED_ENGINE_TOOLS: tuple[str, ...] = (
    "Write", "Edit", "MultiEdit", "NotebookEdit",
    "Artifact",
    "Agent", "Task", "Workflow", "ScheduleWakeup",
    # The engine's own interactive-TUI question tool. Headless it does nothing
    # useful, and the model reaches for this name first out of habit — block
    # it outright so it goes straight to mcp__voitta__ask_user_question
    # instead of burning a round-trip on a denial.
    "AskUserQuestion",
)


def _make_can_use_tool():
    """Per-turn ``can_use_tool``: allow the bridged Voitta tools plus the
    engine-builtin allowlist, deny the rest of the engine's native tools.

    Second layer only. Anything in ``allowed_tools`` never reaches this, and a
    subagent's calls never reach it either — ``_DISALLOWED_ENGINE_TOOLS`` is
    what actually holds the line. This catches tools that are neither allowed
    nor disallowed (a new engine built-in, say), for callers that narrow
    ``allowed_tools``.
    """

    async def _can_use_tool(tool_name: str, tool_input: dict, _ctx) -> Any:
        if tool_name.startswith(f"mcp__{MCP_SERVER_NAME}__") or tool_name in _ALLOWED_ENGINE_TOOLS:
            return PermissionResultAllow()
        return PermissionResultDeny(message=f"{tool_name} is not available in this assistant")

    return _can_use_tool


def _build_options(
    *, system: str, model: str | None, resume: str | None, ctx: ToolCtx, can_use_tool: Any,
    cli_path: str | None = None,
) -> ClaudeAgentOptions:
    server, allowed = build_tool_server(ctx)
    return ClaudeAgentOptions(
        cli_path=cli_path,
        cwd=str(workspace_dir()),
        env=subprocess_env(),
        mcp_servers={MCP_SERVER_NAME: server},
        allowed_tools=[*allowed, *_ALLOWED_ENGINE_TOOLS],
        disallowed_tools=list(_DISALLOWED_ENGINE_TOOLS),
        can_use_tool=can_use_tool,
        system_prompt=system or None,
        model=model or DEFAULT_MODEL,
        resume=resume,
        # Do not load ~/.claude or project .claude config — keep the brain's
        # behaviour fully defined by our system prompt + tool surface. Must be
        # ``[]``: ``None`` means "load every source" (CLI default), which walked
        # up from the workspace cwd and fed the user's own ~/.claude/CLAUDE.md
        # to the brain as project instructions.
        setting_sources=[],
        permission_mode="default",
        # Raw API stream events (thinking/text deltas, live usage) — consumed
        # as TELEMETRY ONLY by the status ticker (phase + counters). Chat
        # content still renders exclusively from complete messages, so a
        # delta can never double-append prose.
        include_partial_messages=True,
    )


async def run_agent_sdk_turn(
    *,
    user_text: str,
    system: str,
    model: str | None,
    resume_session_id: str | None,
    ctx: ToolCtx,
    image_blocks: list[dict[str, Any]] | None = None,
) -> TurnResult:
    """Run one turn; stream output to Chainlit; return the session id.

    ``image_blocks``: attached images as Anthropic base64 image blocks,
    sent inline in the user message (API-flow parity — instant vision).

    Raises :class:`AgentSdkUnavailable` if the engine isn't installed and
    :class:`AgentSdkAuthError` if the subscription token is missing/expired/
    rejected — the caller maps those to the disabled-brain and onboarding
    paths respectively.
    """
    if query is None:
        raise AgentSdkUnavailable("claude-agent-sdk is not installed")

    # ask_user_question plumbing (app.tools.server.ask_user). The tool runs in
    # an SDK-spawned task where Chainlit's contextvar is not guaranteed, so the
    # context captured here rides along in ``ctx.extras`` for it to re-bind;
    # ``wait_state`` flips the ticker to its "waiting" face; ``deadline`` is a
    # one-slot cell for the asyncio.Timeout (filled once the turn starts, read
    # only when a question is asked) so the handler can push the turn ceiling
    # out around a human-paced wait and restore it after.
    try:
        cl_ctx: Any = cl_get_context()
    except Exception:
        cl_ctx = None
    wait_state: dict[str, bool] = {"asking": False}
    deadline: list[Any] = [None]
    ctx.extras["ask_user.cl_ctx"] = cl_ctx
    ctx.extras["ask_user.wait_state"] = wait_state
    ctx.extras["ask_user.deadline"] = deadline
    ctx.extras["ask_user.turn_timeout_s"] = _TURN_TIMEOUT_S
    # Newest engine available (bundled vs system install) — may spawn
    # `claude --version` once, so off the event loop.
    engine = await asyncio.to_thread(engine_cli_path)
    options = _build_options(
        system=system, model=model, resume=resume_session_id, ctx=ctx,
        can_use_tool=_make_can_use_tool(), cli_path=engine,
    )

    streaming_msg: cl.Message | None = None
    steps: dict[str, cl.Step] = {}
    session_id: str | None = resume_session_id
    result_msg: ResultMessage | None = None
    # Live telemetry: the loop mutates `t` (plain assignments), the ticker
    # renders it — single writer on the status step is preserved.
    from app.services.agent_sdk.turn_status import TurnStatus

    t = TurnStatus()

    # One slick, self-animating status line — the only "busy" element. A
    # background ticker spins it and ticks the elapsed/token counters once a
    # second, so the turn stays lively even during the silent thinking gaps
    # between events. It owns the status step exclusively (the main loop only
    # mutates `tokens`), so there's no second writer and no pile of brown
    # half-updated lines. Removed entirely when the turn ends — no footer.
    status = cl.Step(name="Claude Code", type="run")
    status.output = "⠋ Working…"
    await status.send()

    _SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    t0 = time.monotonic()

    async def _ticker() -> None:
        i = 0
        try:
            while True:
                status.output = t.line(_SPIN[i % len(_SPIN)], wait_state["asking"])
                await status.update()
                i += 1
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass

    async def _flush_text() -> None:
        nonlocal streaming_msg
        if streaming_msg is not None:
            await streaming_msg.update()
            streaming_msg = None

    ticker = asyncio.create_task(_ticker())
    # Hold the generator explicitly so we can guarantee it's closed on every
    # exit path — closing it tears down the SDK transport + engine subprocess,
    # so a stuck turn can't linger.
    agen = query(prompt=user_prompt_stream(user_text, image_blocks), options=options)
    timed_out = False
    logger.info(
        "agent_sdk turn start: resume=%s model=%s prompt_chars=%d engine=%s",
        resume_session_id or "-", model or DEFAULT_MODEL, len(user_text), engine or "sdk-default",
    )
    try:
        # asyncio.timeout (3.11+) fires between/after awaits — an agentic loop
        # yields control at each engine round-trip, so the deadline is honoured
        # even mid-turn. (A single synchronous block inside a tool would not be
        # preempted; the heavy tools — e.g. run_script — are already thread
        # off-loaded, so in practice the turn stays interruptible.)
        async with asyncio.timeout(_TURN_TIMEOUT_S) as _turn_deadline:
            # Hand the Timeout to the ask_user_question tool so a pending
            # question can push the ceiling out (and restore it). Without a
            # ceiling there is nothing to extend — and rescheduling would
            # create one.
            deadline[0] = _turn_deadline if _TURN_TIMEOUT_S else None
            async for message in agen:
                if StreamEvent is not None and isinstance(message, StreamEvent):
                    # Telemetry only — phases + live counters for the status
                    # line. Chat content renders exclusively from complete
                    # messages below, so deltas can never double-append.
                    t.on_stream_event(getattr(message, "event", None) or {})
                    continue
                if isinstance(message, AssistantMessage):
                    if getattr(message, "model", None) == SYNTHETIC_MODEL:
                        # Engine-generated, not the model: "No response
                        # requested." (its reply to the "Continue from where
                        # you left off." it injects when resuming an
                        # interrupted turn) or an API-error echo that the
                        # error ResultMessage already surfaces. Pure noise.
                        continue
                    t.on_usage(getattr(message, "usage", None))
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            # Assistant prose — preambles between tool calls AND
                            # the final answer. Each contiguous run is its own
                            # bubble; a tool call closes the current bubble so the
                            # next run starts fresh (nothing is merged or eaten).
                            if not block.text:
                                continue
                            if streaming_msg is None:
                                streaming_msg = cl.Message(content="")
                                await streaming_msg.send()
                            await streaming_msg.stream_token(block.text)
                        elif isinstance(block, ThinkingBlock):
                            # Reasoning isn't shown (summarised/omitted by
                            # default); the ticker already conveys "busy".
                            continue
                        elif isinstance(block, ToolUseBlock):
                            await _flush_text()
                            name = (block.name or "").removeprefix(f"mcp__{MCP_SERVER_NAME}__")
                            t.on_tool_start(name or "tool")
                            step = cl.Step(name=name or "tool", type="tool")
                            try:
                                import json as _json
                                step.input = _truncate(_json.dumps(block.input, ensure_ascii=False, default=str))
                            except Exception:
                                step.input = str(block.input)
                            await step.send()
                            steps[block.id] = step
                elif isinstance(message, UserMessage):
                    # Tool results the engine fed back — attach to their steps.
                    content = message.content
                    blocks = content if isinstance(content, list) else []
                    for block in blocks:
                        if isinstance(block, ToolResultBlock):
                            t.on_tool_result()
                            step = steps.get(block.tool_use_id)
                            if step is not None:
                                step.output = _truncate(_tool_result_text(block.content))
                                if block.is_error:
                                    step.is_error = True
                                await step.update()
                elif isinstance(message, SystemMessage):
                    # The engine's init event announces the session id at turn
                    # START. Latch it as the user's active session right away so
                    # the history dropdown can title an in-flight conversation —
                    # waiting for the ResultMessage meant multi-minute first
                    # turns showed the default label the whole time.
                    sid = (getattr(message, "data", None) or {}).get("session_id")
                    if sid and sid != session_id:
                        session_id = sid
                        logger.info("agent_sdk turn: session id %s", sid)
                        try:
                            from app.services.agent_sdk.selection import set_active
                            set_active(ctx.email, sid)
                        except Exception:
                            logger.exception("set_active at init failed")
                elif isinstance(message, ResultMessage):
                    result_msg = message
                    if message.session_id:
                        session_id = message.session_id

        if result_msg is not None and result_msg.is_error:
            _raise_for_result(result_msg, model)
    except (TimeoutError, asyncio.TimeoutError):
        # Turn exceeded the wall-clock ceiling — treat as a clean, non-fatal end
        # rather than a crash. The finally block closes the generator (killing
        # the engine subprocess); we surface a message below and keep the session
        # id so the user can continue.
        timed_out = True
        logger.warning("agent_sdk turn timed out after %.0fs", _TURN_TIMEOUT_S)
    except CLINotFoundError as exc:
        raise AgentSdkUnavailable(str(exc)) from exc
    except AgentSdkError:
        raise
    except Exception as exc:  # noqa: BLE001 — classify then re-raise
        # The SDK yields an error ``ResultMessage`` and *then* raises a generic
        # "returned an error result" exception on the next iteration. The
        # structured result classifies far more reliably than the exception
        # text, so prefer it when we captured one.
        if result_msg is not None and result_msg.is_error:
            _raise_for_result(result_msg, model)
        text = str(exc).lower()
        if any(h in text for h in _AUTH_HINTS):
            raise AgentSdkAuthError(detail=str(exc)) from exc
        raise AgentSdkError(_explain(str(exc), model)) from exc
    finally:
        # Always tear the engine down — aclose() propagates GeneratorExit into
        # the SDK's query loop, which terminates the subprocess transport. This
        # is what stops a stuck/interactive turn from lingering as a "dead
        # session" that holds the event loop.
        try:
            await agen.aclose()
        except Exception:
            pass
        ticker.cancel()
        try:
            await ticker
        except Exception:
            pass
        await _flush_text()
        # The status line is pure entertainment — drop it when the turn ends.
        try:
            await status.remove()
        except Exception:
            pass

    logger.info(
        "agent_sdk turn end: session=%s elapsed=%.0fs timed_out=%s · %s",
        session_id or "-", time.monotonic() - t0, timed_out,
        t.summary() or "no telemetry",
    )
    if timed_out:
        await cl.Message(
            content=(
                f"⏱️ This turn hit the configured limit of "
                f"{_TURN_TIMEOUT_S:.0f} s (VOITTA_BRAIN_TURN_TIMEOUT_S) and was "
                "stopped. Send another message to continue — the conversation "
                "is preserved."
            ),
        ).send()
        return TurnResult(session_id=session_id, is_error=True)

    return TurnResult(session_id=session_id, is_error=False)


def _raise_for_result(result_msg: ResultMessage, model: str | None = None) -> None:
    """Raise the right typed error for an error ``ResultMessage`` (never returns)."""
    if _is_auth_failure(result_msg):
        raise AgentSdkAuthError(detail=str(result_msg.result or result_msg.errors or ""))
    raise AgentSdkError(_explain(str(result_msg.result or result_msg.errors or "agent turn failed"), model))


# "Claude Code 2.1.277 does not support this model; version 2.1.280 or newer is
# required. Run 'claude update', …" — the engine's gate on newly released models.
_ENGINE_TOO_OLD = re.compile(
    r"Claude Code (\S+) does not support this model; version (\S+) or newer", re.I,
)


def _explain(detail: str, model: str | None) -> str:
    """Rewrite engine errors the user can act on into plain instructions.

    The raw engine text says "run 'claude update', or update the Claude desktop
    app", neither of which reaches the engine *bundled with Voitta*. What does
    work: updating the system Claude Code install (the brain runs whichever
    engine is newer — see config.engine_cli_path) or picking another model.
    """
    m = _ENGINE_TOO_OLD.search(detail)
    if m:
        have, need = m.groups()
        return (
            f"The model **{model or DEFAULT_MODEL}** needs Claude Code {need} or newer, "
            f"but the newest engine Voitta found is {have}. Run `claude update` in a "
            "terminal (Voitta uses it from the next message — no restart), or pick a "
            "different model in ⚙ Settings."
        )
    return detail
