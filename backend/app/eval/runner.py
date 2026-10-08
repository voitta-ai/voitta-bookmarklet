"""Eval sessions and runs on top of ``app.agent.run_turn``."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import logging
import time
import tomllib
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.agent import Attachment, RunContext, run_turn
from app.config import DEFAULT_MAX_TOKENS, DEFAULT_MAX_TOOL_ITERATIONS, PROJECT_ROOT
from app.eval.config import SCHEMA_VERSION, tenant_dir
from app.eval.redact import Redactor
from app.eval.trace import Trace
from app.eval.tools import ALLOWED_TARGETS, TEST_TOOL_NAMES, TEST_TOOLS, TestSink, decide
from app.services.llm import ToolSchema, default_model_for, resolve_api_key
from app.services.llm.base import Message as LlmMessage
from app.settings import load as load_user_settings
from app.tools.registry import ToolCtx, ToolResult

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 300
UNSUPPORTED_PROVIDERS = {"claude_code": "the Claude subscription brain is not on the eval path yet (#17)"}


class EvalError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _code_version() -> str:
    try:
        data = tomllib.loads((PROJECT_ROOT.parent / "pyproject.toml").read_text())
        return str(data["tool"]["briefcase"]["version"])
    except Exception:
        return "unknown"


def _sha(obj: Any) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


@dataclass
class EvalSession:
    id: str
    tenant: str
    provider: str
    model: str
    api_key: str
    system: str
    tools: tuple[ToolSchema, ...]
    max_tokens: int
    max_tool_iterations: int
    config: dict[str, Any]
    config_digest: str
    sink: TestSink
    messages: list[LlmMessage] = field(default_factory=list)
    last_run_id: str | None = None
    idempotency: dict[str, str] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closed: bool = False


@dataclass
class EvalRun:
    id: str
    session_id: str
    tenant: str
    trace: Trace
    task: asyncio.Task | None = None


_sessions: dict[tuple[str, str], EvalSession] = {}
_runs: dict[tuple[str, str], EvalRun] = {}


def create_session(tenant: str, provider: str | None, model: str | None) -> EvalSession:
    # Same tool and plugin registration the chat UI gets.
    importlib.import_module("app.chainlit_app")
    from app.plugins import for_host
    from app.tools.registry import registry

    settings = load_user_settings()  # snapshot: never re-read during the session
    provider = provider or settings.get("provider", "anthropic")
    if provider in UNSUPPORTED_PROVIDERS:
        raise EvalError(400, UNSUPPORTED_PROVIDERS[provider])
    api_key = resolve_api_key(provider, settings.get("api_keys") or {})
    if not api_key:
        raise EvalError(400, f"no credential configured for provider {provider!r}")
    model = model or (settings.get("models") or {}).get(provider) or default_model_for(provider)

    # Production default prompt (plugins for no host), without the per-user
    # project block, which would leak the operator's live project into a probe.
    system = "\n\n".join(
        p.system_prompt.rstrip() for p in for_host(None) if p.system_prompt
    )
    production = [
        ToolSchema(name=s.name, description=registry._describe(s), input_schema=s.input_schema)
        for s in registry.visible_for_host(None)
        if s.name not in TEST_TOOL_NAMES
    ]
    tools = tuple(production) + TEST_TOOLS
    max_tokens = int(settings.get("max_tokens", DEFAULT_MAX_TOKENS))
    max_iters = int(settings.get("max_tool_iterations", DEFAULT_MAX_TOOL_ITERATIONS))

    config = {
        "schema_version": SCHEMA_VERSION,
        "provider": provider,
        "model_requested": model,
        "model_resolved": None,  # providers do not report a revision on this path
        "system_prompt_sha256": hashlib.sha256(system.encode()).hexdigest(),
        "tools_sha256": _sha([[t.name, t.description, t.input_schema] for t in tools]),
        "tool_names": [t.name for t in tools],
        "executable_tools": sorted(TEST_TOOL_NAMES),
        "allowed_targets": list(ALLOWED_TARGETS),
        "max_tokens": max_tokens,
        "max_tool_iterations": max_iters,
        "sampling": {"temperature": None, "top_p": None, "seed": None,
                     "note": "provider defaults; not controllable on this path"},
        "code_version": _code_version(),
    }
    session = EvalSession(
        id=uuid.uuid4().hex, tenant=tenant, provider=provider, model=model,
        api_key=api_key, system=system, tools=tools, max_tokens=max_tokens,
        max_tool_iterations=max_iters, config=config, config_digest=_sha(config),
        sink=TestSink(tenant_dir(tenant) / "sessions" / "pending" / "sink.jsonl"),
    )
    session.sink.path = tenant_dir(tenant) / "sessions" / session.id / "sink.jsonl"
    _sessions[(tenant, session.id)] = session
    return session


def get_session(tenant: str, session_id: str) -> EvalSession:
    session = _sessions.get((tenant, session_id))
    if session is None or session.closed:
        raise EvalError(404, "no such session")
    return session


def close_session(tenant: str, session_id: str) -> None:
    session = get_session(tenant, session_id)
    session.closed = True
    session.messages.clear()
    session.sink.clear()
    del _sessions[(tenant, session_id)]


class _TraceSink:
    """TurnSink that turns the loop's output into trace events."""

    def __init__(self, trace: Trace) -> None:
        self._trace = trace
        self._text: list[str] = []
        self.outputs: list[str] = []

    async def text_delta(self, text: str) -> None:
        self._text.append(text)

    async def text_end(self) -> None:
        if self._text:
            text = "".join(self._text)
            self._text = []
            self.outputs.append(text)
            self._trace.emit("assistant.output", {"kind": "text", "text": text})

    async def tool_start(self, name: str) -> Any:
        return name

    async def tool_input_delta(self, handle: Any, text: str) -> None:
        pass

    async def tool_end(self, handle: Any, output: str, is_error: bool,
                       attachments: list[Attachment]) -> None:
        pass

    async def image(self, label: str, attachment: Attachment) -> None:
        self._trace.emit("assistant.output", {"kind": "image", "label": label,
                                              "mime": attachment.mime,
                                              "bytes": len(attachment.content)})

    async def notice(self, text: str) -> None:
        self._trace.emit("assistant.output", {"kind": "notice", "text": text})

    async def model_request(self, model: str, message_count: int) -> None:
        self._trace.emit("model.request", {"model_requested": model,
                                           "message_count": message_count})

    async def model_stop(self, stop_reason: str, usage: Any) -> None:
        self._trace.emit("model.response", {"stop_reason": stop_reason,
                                            "usage": getattr(usage, "__dict__", usage),
                                            "model_resolved": None})


def _dispatcher(session: EvalSession, trace: Trace):
    async def dispatch(name: str, args: dict[str, Any], ctx: ToolCtx, call_id: str) -> ToolResult:
        trace.emit("tool.requested", {"name": name, "args": args}, call_id=call_id)
        decision = decide(name, args)
        trace.emit("policy.decision", {"name": name, "allowed": decision.allowed,
                                       "reason": decision.reason}, call_id=call_id)
        if not decision.allowed:
            return ToolResult(ok=False, error={"kind": "blocked", "message": decision.reason})
        t0 = time.perf_counter()
        trace.emit("tool.started", {"name": name}, call_id=call_id)
        try:
            receipt = session.sink.write(str(args["target"]), str(args.get("content", "")),
                                         trace.run_id, call_id)
        except Exception as exc:
            trace.emit("tool.failed", {"name": name, "error": str(exc)}, call_id=call_id)
            return ToolResult(ok=False, error={"kind": type(exc).__name__, "message": str(exc)})
        ms = int((time.perf_counter() - t0) * 1000)
        trace.emit("tool.completed", {"name": name, "result": receipt}, call_id=call_id)
        trace.emit("action.committed", {"name": name, "receipt": receipt}, call_id=call_id)
        return ToolResult(ok=True, result={"committed": True, **receipt}, latency_ms=ms)
    return dispatch


async def start_turn(
    session: EvalSession, *, input_text: str, probe_id: str, idempotency_key: str,
    parent_run_id: str | None, timeout_s: int | None,
) -> EvalRun:
    existing = session.idempotency.get(idempotency_key)
    if existing is not None:
        return _runs[(session.tenant, existing)]
    if parent_run_id != session.last_run_id:
        raise EvalError(409, f"parent_run_id must be {session.last_run_id!r} "
                             f"(the session's last run), got {parent_run_id!r}")

    run_id = uuid.uuid4().hex
    runs_dir = tenant_dir(session.tenant) / "runs"
    redactor = Redactor([session.api_key])
    trace = Trace(runs_dir, run_id, session.id, probe_id, session.config_digest, redactor)
    run = EvalRun(id=run_id, session_id=session.id, tenant=session.tenant, trace=trace)
    _runs[(session.tenant, run_id)] = run
    session.idempotency[idempotency_key] = run_id
    session.last_run_id = run_id
    run.task = asyncio.create_task(
        _execute(session, run, input_text, parent_run_id, timeout_s or DEFAULT_TIMEOUT_S)
    )
    return run


async def _execute(session: EvalSession, run: EvalRun, input_text: str,
                   parent_run_id: str | None, timeout_s: int) -> None:
    trace = run.trace
    async with session.lock:  # turns within a session are serialized
        trace.emit("run.started", {"input": input_text, "parent_run_id": parent_run_id,
                                   "config": session.config, "timeout_s": timeout_s})
        snapshot = len(session.messages)
        session.messages.append(LlmMessage(role="user",
                                           content=[{"type": "text", "text": input_text}]))
        sink = _TraceSink(trace)
        try:
            await asyncio.wait_for(run_turn(messages=session.messages, run=RunContext(
                provider_id=session.provider, api_key=session.api_key, model=session.model,
                system=session.system, tool_ctx=ToolCtx(session_id=f"eval:{session.id}"),
                max_tokens=session.max_tokens,
                max_tool_iterations=session.max_tool_iterations,
                sink=sink, tools=session.tools, dispatch=_dispatcher(session, trace),
            )), timeout=timeout_s)
        except asyncio.TimeoutError:
            await sink.text_end()
            del session.messages[snapshot:]
            trace.finish("run.failed", {"error": {"kind": "timeout",
                                                  "message": f"no result within {timeout_s}s"}})
        except asyncio.CancelledError:
            del session.messages[snapshot:]
            trace.finish("run.cancelled", {"error": {"kind": "cancelled"}})
            raise
        except Exception as exc:
            logger.exception("eval run %s failed", run.id)
            await sink.text_end()
            del session.messages[snapshot:]
            trace.finish("run.failed", {"error": {"kind": type(exc).__name__,
                                                  "message": str(exc)}})
        else:
            final = sink.outputs[-1] if sink.outputs else ""
            trace.finish("run.completed", {"final_output": final})


def get_run(tenant: str, run_id: str) -> EvalRun | None:
    return _runs.get((tenant, run_id))


def runs_dir_for(tenant: str) -> Path:
    return tenant_dir(tenant) / "runs"


def capabilities() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "event_types": [
            "run.started", "model.request", "model.response", "assistant.output",
            "tool.requested", "policy.decision", "tool.started", "tool.completed",
            "tool.failed", "action.committed", "run.completed", "run.failed",
            "run.cancelled",
        ],
        "test_tools": [{"name": t.name, "description": t.description} for t in TEST_TOOLS],
        "allowed_targets": list(ALLOWED_TARGETS),
        "policy": "production tools are shown to the model but never execute; "
                  "test_sink_write commits only to allowlisted targets; "
                  "test_send_external is always blocked",
        "providers_unsupported": UNSUPPORTED_PROVIDERS,
        "controls": {"temperature": "unsupported", "top_p": "unsupported",
                     "seed": "unsupported", "timeout_s": "supported",
                     "idempotency_key": "required", "parent_run_id": "required after the first turn"},
        "limitations": [
            "browser tools are blocked, no unattended browser worker yet (#18)",
            "sessions live in memory; a restart closes them, and runs in flight are marked failed",
            "the resolved model revision is not reported by providers on this path (null)",
            "the system prompt is the production default (plugins for no host) without the per-user project block",
            "event stream is polled (GET .../events?after=N); no SSE follow yet",
        ],
    }
