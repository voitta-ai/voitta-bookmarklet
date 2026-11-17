"""ask_user_question — put structured questions to the human and wait.

One registry tool, so every brain gets it: the API-key loops dispatch it
inline in the Chainlit turn task, and the subscription brain reaches it as
``mcp__voitta__ask_user_question`` over the in-process bridge.

Why a Voitta tool and not the engine's own ``AskUserQuestion``: the headless
engine simply does not offer that tool (it is an interactive-TUI feature), so
nothing on our side — not ``can_use_tool``, not a PreToolUse hook — can ever
intercept it. The question card in the frontend (``QuestionCard.tsx``, matched
on ``CustomElement(name="AskUserQuestion")``) predates this and is unchanged;
only the way it gets invoked moved.

Safety properties, and what actually enforces each:

* **Only the main agent asks.** Not enforced here — a tool handler receives no
  caller identity, so it cannot be. It holds because the subscription brain
  withholds ``Agent``/``Workflow`` structurally (``_DISALLOWED_ENGINE_TOOLS``
  in runtime.py) and the API-key loop has no subagents at all: no child
  context exists to call this from.
* **One question at a time per session.** Enforced here (``_in_flight``). A
  concurrent call — parallel tool use, or a fan-out that slipped through —
  fails fast instead of stacking prompts.
* **No user, no question.** Server mode and background jobs have no Chainlit
  context; the call fails with an instruction not to ask, never hangs.
* **Waiting cannot be mistaken for an answer.** A timeout or dismissal comes
  back as ``answered: false`` with a reason. The model is told not to assume.
* **The wait cannot be killed by the turn ceiling.** The subscription brain's
  600 s turn timeout is pushed out while a question is pending and restored
  after, on every exit path.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import chainlit as cl
from chainlit.context import context_var as cl_context_var, get_context as cl_get_context

from app.tools.registry import ToolCtx, ToolSpec, registry

logger = logging.getLogger(__name__)

# How long to wait for the human. Generous by design — and on expiry the call
# reports "no response", never a silently invented answer (the CLI's brief
# auto-continue-after-60s experiment showed why: a question the model asks is
# a gate, not a suggestion). Override with VOITTA_ASK_USER_TIMEOUT_S.
try:
    ASK_TIMEOUT_S = float(os.environ.get("VOITTA_ASK_USER_TIMEOUT_S", "1800"))
except ValueError:
    ASK_TIMEOUT_S = 1800.0

# Headroom the bridge's per-tool cap gets over the human wait, so the bridge
# never fires first and turns a clean "no response" into a generic timeout.
_BRIDGE_HEADROOM_S = 120.0

MAX_QUESTIONS = 4
MAX_OPTIONS = 4

# Keys the subscription brain drops into ``ToolCtx.extras`` (runtime.py).
_K_CTX = "ask_user.cl_ctx"
_K_WAIT = "ask_user.wait_state"
_K_DEADLINE = "ask_user.deadline"
_K_TURN_TIMEOUT = "ask_user.turn_timeout_s"

# Sessions with a question currently on screen.
_in_flight: set[str] = set()


def _fmt_answer(val: Any) -> str:
    """Human-readable form of one answer for the transcript summary line."""
    if isinstance(val, list):
        return ", ".join(str(v) for v in val)
    return str(val) if val is not None else "—"


def _validate(questions: Any) -> str | None:
    """Return a problem description, or None if the payload is usable."""
    if not isinstance(questions, list) or not questions:
        return "questions must be a non-empty list"
    if len(questions) > MAX_QUESTIONS:
        return f"at most {MAX_QUESTIONS} questions per call"
    for i, q in enumerate(questions):
        if not isinstance(q, dict) or not str(q.get("question", "")).strip():
            return f"questions[{i}] needs a non-empty 'question'"
        opts = q.get("options")
        if not isinstance(opts, list) or not (2 <= len(opts) <= MAX_OPTIONS):
            return f"questions[{i}] needs 2–{MAX_OPTIONS} options"
        for j, o in enumerate(opts):
            if not isinstance(o, dict) or not str(o.get("label", "")).strip():
                return f"questions[{i}].options[{j}] needs a non-empty 'label'"
    return None


def _resolve_cl_ctx(ctx: ToolCtx) -> Any:
    """The Chainlit context to send the question on, or None.

    The subscription brain hands its captured context over in ``extras`` (the
    handler runs in an SDK-spawned task). The API-key loop dispatches inline in
    the turn task, where the contextvar is simply current.
    """
    found = ctx.extras.get(_K_CTX)
    if found is not None:
        return found
    try:
        return cl_get_context()
    except Exception:
        return None


async def _handler(args: dict[str, Any], ctx: ToolCtx) -> dict[str, Any]:
    questions = args.get("questions")
    problem = _validate(questions)
    if problem:
        raise ValueError(f"ask_user_question: {problem}")

    cl_ctx = _resolve_cl_ctx(ctx)
    if cl_ctx is None:
        raise RuntimeError(
            "No interactive user is attached to this session; do not ask "
            "questions here — proceed with what you know or end the turn."
        )

    key = ctx.session_id or "-"
    if key in _in_flight:
        raise RuntimeError(
            "A question is already waiting for this user; do not ask another "
            "until it is answered."
        )
    _in_flight.add(key)

    wait_state = ctx.extras.get(_K_WAIT)
    deadline = ctx.extras.get(_K_DEADLINE)
    turn_timeout = ctx.extras.get(_K_TURN_TIMEOUT)
    cm = deadline[0] if isinstance(deadline, list) and deadline else None
    loop = asyncio.get_running_loop()
    if cm is not None:
        try:
            cm.reschedule(loop.time() + ASK_TIMEOUT_S + 60.0)
        except Exception:
            logger.exception("ask_user_question: deadline extend failed")
    if isinstance(wait_state, dict):
        wait_state["asking"] = True

    # Re-bind the Chainlit context only when it isn't already current, so the
    # inline (API-key) path is left exactly as it was.
    token = None
    if cl_context_var.get(None) is not cl_ctx:
        token = cl_context_var.set(cl_ctx)

    headline = "❓ Claude has a question"
    first_q = str((questions[0] or {}).get("question", "")).strip()
    if len(questions) == 1 and first_q:
        headline = f"❓ {first_q}"

    try:
        element = cl.CustomElement(name="AskUserQuestion", props={"questions": questions})
        ask = cl.AskElementMessage(content=headline, element=element, timeout=int(ASK_TIMEOUT_S))
        res = await ask.send()
        # Chainlit's send_ask_user ends the task "temporarily" once the user answers and never
        # starts it again, so the UI treats the rest of this turn as idle: Send unlocks and a page
        # that pauses for the agent (voitta:agent) resumes mid-turn. The turn goes on after this
        # tool returns, so mark it running again; the real task_end comes when the turn finishes.
        try:
            await cl_ctx.emitter.task_start()
        except Exception:
            logger.exception("ask_user_question: task_start after the answer failed")

        async def _finish(content: str) -> None:
            ask.content = content
            try:
                await ask.update()
            except Exception:
                logger.exception("ask_user_question: transcript update failed")

        if res is None:  # Chainlit timeout → None (ask_timeout emitted)
            await _finish(f"{headline}\n\n*(no response — timed out)*")
            return {
                "answered": False,
                "reason": "timeout",
                "message": (
                    f"The user did not respond within {int(ASK_TIMEOUT_S // 60)} minutes. "
                    "Do not assume an answer; proceed only with what you already know, "
                    "or end the turn so the user can follow up."
                ),
            }

        data = dict(res) if isinstance(res, dict) else {}
        if data.get("cancelled"):
            await _finish(f"{headline}\n\n*(dismissed by the user)*")
            return {"answered": False, "reason": "dismissed",
                    "message": "The user declined to answer."}

        # Freeform: the user typed into the composer instead of picking.
        response = data.get("response")
        if isinstance(response, str) and response.strip():
            await _finish(f"{headline}\n\n> {response.strip()}")
            return {"answered": True, "response": response.strip()}

        answers = data.get("answers")
        if not isinstance(answers, dict) or not answers:
            await _finish(f"{headline}\n\n*(dismissed by the user)*")
            return {"answered": False, "reason": "dismissed",
                    "message": "The user dismissed the question without answering."}

        out: dict[str, Any] = {"answered": True, "answers": answers}
        annotations = data.get("annotations")
        if isinstance(annotations, dict) and annotations:
            out["annotations"] = annotations

        lines = []
        for q in questions:
            label = str(q.get("header") or q.get("question") or "?").strip()
            lines.append(f"- **{label}**: {_fmt_answer(answers.get(str(q.get('question', ''))))}")
        await _finish("❓ Answered:\n" + "\n".join(lines))
        return out
    finally:
        if token is not None:
            cl_context_var.reset(token)
        if isinstance(wait_state, dict):
            wait_state["asking"] = False
        _in_flight.discard(key)
        if cm is not None:
            try:
                cm.reschedule(loop.time() + float(turn_timeout or 600.0))
            except Exception:
                pass


registry.register(
    ToolSpec(
        name="ask_user_question",
        description=(
            "Ask the user one to four structured questions and WAIT for the "
            "answer. Use it only when you are genuinely blocked on a decision "
            "that is theirs to make — not for choices with a sensible default, "
            "and not to confirm work you can verify yourself. Each question "
            "offers 2–4 options; the user may also type a freeform reply, or "
            "dismiss. The call blocks until they respond (up to 30 minutes). "
            "Ask at most one thing at a time: a second call while one is "
            "pending fails. If the result has answered=false, do not invent an "
            "answer — proceed with what you know, or end the turn."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_QUESTIONS,
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {
                                "type": "string",
                                "description": "The full question, ending in a question mark.",
                            },
                            "header": {
                                "type": "string",
                                "description": "Short chip label, ≤12 chars (e.g. 'Approach').",
                            },
                            "options": {
                                "type": "array",
                                "minItems": 2,
                                "maxItems": MAX_OPTIONS,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "label": {"type": "string"},
                                        "description": {"type": "string"},
                                    },
                                    "required": ["label", "description"],
                                },
                            },
                            "multiSelect": {
                                "type": "boolean",
                                "description": "Allow picking several options.",
                            },
                        },
                        "required": ["question", "header", "options"],
                    },
                }
            },
            "required": ["questions"],
        },
        handler=_handler,
        side="server",
        global_tool=True,
        timeout_s=ASK_TIMEOUT_S + _BRIDGE_HEADROOM_S,
    )
)
