"""ask_user_question: the one tool that waits on a human.

The Chainlit ask channel is stubbed — these pin the handler's own contract:
validation, the no-user and single-flight guards, the four ways a wait can
end, and the turn-deadline bookkeeping the subscription brain relies on. The
frontend card is real and untouched; nothing here renders it.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from chainlit.context import context_var as cl_context_var

import app.tools.load  # noqa: F401  — registers the tool
from app.services.agent_sdk import bridge
from app.tools.registry import ToolCtx, ToolSpec, registry
from app.tools.server import ask_user

Q = [{"question": "Which approach?", "header": "Approach",
      "options": [{"label": "A", "description": "a"}, {"label": "B", "description": "b"}]}]


class _FakeAsk:
    """Stand-in for cl.AskElementMessage. ``reply`` is what send() returns;
    ``gate`` (if set) makes send() block until it is released."""

    reply: Any = None
    gate: asyncio.Event | None = None
    sent: list["_FakeAsk"] = []

    def __init__(self, content: str, element: Any, timeout: int) -> None:
        self.content, self.element, self.timeout = content, element, timeout
        self.updates: list[str] = []
        _FakeAsk.sent.append(self)

    async def send(self) -> Any:
        if _FakeAsk.gate is not None:
            await _FakeAsk.gate.wait()
        return _FakeAsk.reply

    async def update(self) -> None:
        self.updates.append(self.content)


class _FakeDeadline:
    def __init__(self) -> None:
        self.when: list[float] = []

    def reschedule(self, when: float) -> None:
        self.when.append(when)


@pytest.fixture(autouse=True)
def _stub_chainlit(monkeypatch: pytest.MonkeyPatch):
    _FakeAsk.reply, _FakeAsk.gate, _FakeAsk.sent = None, None, []
    monkeypatch.setattr(ask_user.cl, "AskElementMessage", _FakeAsk)
    monkeypatch.setattr(ask_user.cl, "CustomElement",
                        lambda name, props: {"name": name, "props": props})
    ask_user._in_flight.clear()
    yield
    ask_user._in_flight.clear()


def _ctx(with_user: bool = True, **extras: Any) -> ToolCtx:
    ex: dict[str, Any] = dict(extras)
    if with_user:
        ex.setdefault(ask_user._K_CTX, object())  # any non-None context object
    return ToolCtx(session_id="sess-1", host=None, email=None, extras=ex)


# -- registration -------------------------------------------------------------

def test_registered_globally_with_a_human_scale_timeout() -> None:
    spec = registry.get("ask_user_question")
    assert spec is not None
    assert spec.global_tool is True
    assert spec.timeout_s is not None and spec.timeout_s > ask_user.ASK_TIMEOUT_S
    assert spec.input_schema["properties"]["questions"]["maxItems"] == ask_user.MAX_QUESTIONS


# -- guards -------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    None, [], "x",
    [{"question": "", "header": "h", "options": [{"label": "a"}, {"label": "b"}]}],
    [{"question": "q?", "header": "h", "options": [{"label": "only-one"}]}],
    [{"question": "q?", "header": "h", "options": [{"label": ""}, {"label": "b"}]}],
    [dict(Q[0])] * (ask_user.MAX_QUESTIONS + 1),
])
async def test_malformed_payload_is_rejected_before_anything_is_shown(bad: Any) -> None:
    with pytest.raises(ValueError, match="ask_user_question"):
        await ask_user._handler({"questions": bad}, _ctx())
    assert _FakeAsk.sent == []


@pytest.mark.asyncio
async def test_no_interactive_user_fails_fast_and_tells_the_model_not_to_ask() -> None:
    """Server mode / background jobs: no Chainlit context anywhere."""
    assert cl_context_var.get(None) is None  # sanity: not inside a session
    with pytest.raises(RuntimeError, match="do not ask"):
        await ask_user._handler({"questions": Q}, _ctx(with_user=False))
    assert _FakeAsk.sent == []


@pytest.mark.asyncio
async def test_second_question_while_one_is_pending_is_refused() -> None:
    """Single-flight per session — parallel tool calls cannot stack prompts."""
    _FakeAsk.gate = asyncio.Event()
    first = asyncio.create_task(ask_user._handler({"questions": Q}, _ctx()))
    await asyncio.sleep(0)  # let it reach send()
    assert "sess-1" in ask_user._in_flight

    with pytest.raises(RuntimeError, match="already waiting"):
        await ask_user._handler({"questions": Q}, _ctx())

    _FakeAsk.reply = {"answers": {"Which approach?": "A"}}
    _FakeAsk.gate.set()
    assert (await first)["answered"] is True
    assert "sess-1" not in ask_user._in_flight  # released on exit
    assert len(_FakeAsk.sent) == 1


# -- the four ways a wait ends ------------------------------------------------

@pytest.mark.asyncio
async def test_answer_is_returned_verbatim_with_annotations() -> None:
    _FakeAsk.reply = {"answers": {"Which approach?": "B"}, "annotations": {"Which approach?": "n"}}
    out = await ask_user._handler({"questions": Q}, _ctx())
    assert out == {"answered": True, "answers": {"Which approach?": "B"},
                   "annotations": {"Which approach?": "n"}}
    assert _FakeAsk.sent[0].updates[-1].startswith("❓ Answered:")


@pytest.mark.asyncio
async def test_freeform_reply_is_returned_as_response() -> None:
    _FakeAsk.reply = {"response": "  neither, do C  "}
    out = await ask_user._handler({"questions": Q}, _ctx())
    assert out == {"answered": True, "response": "neither, do C"}


@pytest.mark.asyncio
async def test_timeout_is_reported_not_invented() -> None:
    """None from Chainlit = ask_timeout. Must come back answered=false with
    an instruction, never a made-up answer."""
    _FakeAsk.reply = None
    out = await ask_user._handler({"questions": Q}, _ctx())
    assert out["answered"] is False and out["reason"] == "timeout"
    assert "Do not assume an answer" in out["message"]
    assert "timed out" in _FakeAsk.sent[0].updates[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [{"cancelled": True}, {}, {"answers": {}}])
async def test_dismissal_is_reported(reply: dict) -> None:
    _FakeAsk.reply = reply
    out = await ask_user._handler({"questions": Q}, _ctx())
    assert out["answered"] is False and out["reason"] == "dismissed"


# -- turn-deadline bookkeeping (subscription brain) ---------------------------

@pytest.mark.asyncio
async def test_turn_ceiling_is_extended_during_the_wait_and_restored_after() -> None:
    dl = _FakeDeadline()
    wait_state = {"asking": False}
    _FakeAsk.gate = asyncio.Event()
    task = asyncio.create_task(ask_user._handler(
        {"questions": Q},
        _ctx(**{ask_user._K_DEADLINE: [dl], ask_user._K_WAIT: wait_state,
                ask_user._K_TURN_TIMEOUT: 600.0}),
    ))
    await asyncio.sleep(0)
    now = asyncio.get_running_loop().time()
    assert wait_state["asking"] is True
    assert len(dl.when) == 1 and dl.when[0] > now + ask_user.ASK_TIMEOUT_S  # pushed out

    _FakeAsk.reply = {"cancelled": True}
    _FakeAsk.gate.set()
    await task
    assert wait_state["asking"] is False
    assert len(dl.when) == 2 and dl.when[1] - now < 600.0 + 5.0  # back to the turn ceiling


@pytest.mark.asyncio
async def test_deadline_is_restored_even_when_the_ui_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dl = _FakeDeadline()

    async def boom(self):
        raise RuntimeError("socket gone")

    monkeypatch.setattr(_FakeAsk, "send", boom)
    with pytest.raises(RuntimeError, match="socket gone"):
        await ask_user._handler({"questions": Q}, _ctx(**{ask_user._K_DEADLINE: [dl]}))
    assert len(dl.when) == 2
    assert "sess-1" not in ask_user._in_flight


@pytest.mark.asyncio
async def test_works_without_any_sdk_extras() -> None:
    """The API-key loop passes nothing in extras; the tool must not require it."""
    fake_ctx = object()
    token = cl_context_var.set(fake_ctx)  # inline path: contextvar is current
    try:
        _FakeAsk.reply = {"answers": {"Which approach?": "A"}}
        out = await ask_user._handler(
            {"questions": Q}, ToolCtx(session_id="s2", host=None, email=None, extras={})
        )
        assert out["answered"] is True
        assert cl_context_var.get(None) is fake_ctx  # not clobbered
    finally:
        cl_context_var.reset(token)


# -- the bridge honours a per-spec timeout ------------------------------------

@pytest.mark.asyncio
async def test_bridge_uses_spec_timeout_over_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 150 s default would cut a human wait short; ``timeout_s`` must win."""
    monkeypatch.setattr(bridge, "_TOOL_TIMEOUT_S", 0.05)

    async def slow(args, ctx):
        await asyncio.sleep(0.2)
        return {"done": True}

    # Add two throwaway specs and remove exactly those afterwards. Resetting
    # the registry would strip the real tools for every later test — their
    # ``register()`` calls run at submodule import and cannot be replayed by
    # a module reload.
    saved = dict(registry._tools)
    try:
        registry.register(ToolSpec(name="slow_default", description="d", input_schema={"type": "object"},
                                   handler=slow))
        registry.register(ToolSpec(name="slow_override", description="d", input_schema={"type": "object"},
                                   handler=slow, timeout_s=5.0))
        ctx = ToolCtx(session_id="s", host=None, email=None, extras={})

        default = await bridge._make_tool(registry.get("slow_default"), ctx).handler({})
        assert default.get("isError") is True and "timeout" in default["content"][0]["text"]

        override = await bridge._make_tool(registry.get("slow_override"), ctx).handler({})
        assert override.get("isError") is not True
    finally:
        registry._tools.clear()
        registry._tools.update(saved)


# -- the turn keeps running after the answer --------------------------------------

class _Emitter:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def task_start(self) -> None:
        self.calls.append("task_start")


class _ClCtx:
    def __init__(self) -> None:
        self.emitter = _Emitter()


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [
    {"answers": {"Which approach?": "A"}},       # picked
    {"response": "do C"},                        # typed
    {"cancelled": True},                         # dismissed
    None,                                        # timed out
])
async def test_turn_is_marked_running_again_after_the_ask(reply: Any) -> None:
    """Chainlit's send_ask_user ends the task once the user answers and never restarts it; the UI
    would then treat the rest of the turn as idle (Send unlocked, voitta:agent idle → a paused page
    simulation resumes mid-turn). The tool restarts it, whatever the outcome."""
    _FakeAsk.reply = reply
    cl = _ClCtx()
    await ask_user._handler({"questions": Q}, _ctx(**{ask_user._K_CTX: cl}))
    assert cl.emitter.calls == ["task_start"]
