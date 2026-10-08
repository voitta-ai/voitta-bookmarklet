"""Chainlit app: ``@cl.on_chat_start`` + ``@cl.on_message`` glue.

The agent loop lives in :mod:`app.agent`; this module is wiring only.
Importing it has side effects (cl decorator registration).
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any

import chainlit as cl
from chainlit.types import ThreadDict

from app.agent import ChainlitSink, RunContext, run_turn
from app.config import DEFAULT_MAX_TOKENS, DEFAULT_MAX_TOOL_ITERATIONS
from app.plugins import for_host, load_all
from app.services.llm import resolve_api_key
from app.services.llm.base import Message as LlmMessage
from app.settings import load as load_user_settings
from app.tools.registry import ToolCtx
import app.tools.load  # noqa: F401  — registers core tools FIRST

logger = logging.getLogger(__name__)


# ── No-auth single-user resume_thread patch ───────────────────────────────
# Chainlit's resume_thread requires session.user AND checks that the thread's
# userIdentifier matches session.user.identifier. In no-auth mode both fail.
# We replace resume_thread entirely with a version that skips user checks —
# safe for a single-user local deployment.

import json as _json
import chainlit.socket as _cl_socket

async def _patched_resume_thread(session):
    from chainlit.data import get_data_layer
    from chainlit.socket import user_sessions
    data_layer = get_data_layer()
    if not data_layer or not session.thread_id_to_resume:
        return
    thread = await data_layer.get_thread(thread_id=session.thread_id_to_resume)
    if not thread:
        logger.warning("resume_thread: thread %s not found", session.thread_id_to_resume)
        return
    logger.info("resume_thread: resuming thread %s userIdentifier=%s", thread.get("id"), thread.get("userIdentifier"))
    metadata = thread.get("metadata") or {}
    if isinstance(metadata, str):
        metadata = _json.loads(metadata)
    user_sessions[session.id] = metadata.copy()
    if chat_profile := metadata.get("chat_profile"):
        session.chat_profile = chat_profile
    if chat_settings := metadata.get("chat_settings"):
        session.chat_settings = chat_settings

    # assistant_message steps are stored with a parentId pointing to a run-step
    # UUID that is not present in the resumed thread. The react-client's e$()
    # function tries to nest them under the missing parent and silently drops
    # them. Clear parentId on assistant/user message steps so they render as
    # top-level messages.
    for step in thread.get("steps") or []:
        if step.get("type") in ("assistant_message", "user_message"):
            step["parentId"] = None

    return thread

_cl_socket.resume_thread = _patched_resume_thread

def patch_sio_connect():
    """Patch sio 'connect' and 'connection_successful' handlers.

    - connect: update thread_id_to_resume on restored sessions from auth payload
    - connection_successful: replace resume_thread call with our no-auth version
      (patching _cl_socket.resume_thread doesn't work because connection_successful
      calls it as a bare local name captured at definition time)

    Must be called AFTER mount_chainlit. Called from main.py.
    """
    import sys
    import asyncio
    import chainlit.socket as cl_socket

    # ── patch connect ──────────────────────────────────────────────────────
    orig_connect = cl_socket.sio.handlers.get("/", {}).get("connect")
    if not orig_connect:
        logger.error("patch_sio_connect: no connect handler found")
        return

    async def _patched_connect(sid, environ, auth):
        from chainlit.session import WebsocketSession
        result = await orig_connect(sid, environ, auth)
        thread_id = (auth.get("threadId") or None) if auth else None
        session = WebsocketSession.get(sid)
        if session and thread_id and thread_id != session.thread_id_to_resume:
            session.thread_id_to_resume = thread_id
            session.thread_id = thread_id
        return result

    cl_socket.sio.handlers["/"]["connect"] = _patched_connect

    # ── patch connection_successful ────────────────────────────────────────
    # Replace the whole handler so we can use _patched_resume_thread instead
    # of the module-local resume_thread that ignores our monkey-patch.
    orig_conn_successful = cl_socket.sio.handlers.get("/", {}).get("connection_successful")
    if not orig_conn_successful:
        logger.error("patch_sio_connect: no connection_successful handler found")
        return

    async def _patched_connection_successful(sid):
        import traceback as _traceback
        from chainlit.context import init_ws_context
        from chainlit.config import config
        from chainlit.chat_context import chat_context
        from chainlit.message import Message

        try:
            context = init_ws_context(sid)
            await context.emitter.task_end()
            await context.emitter.clear("clear_ask")
            await context.emitter.clear("clear_call_fn")

            # Check thread_id_to_resume FIRST — a reconnect with a new threadId must
            # always resume, even if the session was restored (same cookie) and
            # has_first_interaction is still False from the previous connection.
            if context.session.thread_id_to_resume and config.code.on_chat_resume:
                thread = await _patched_resume_thread(context.session)
                if thread:
                    context.session.has_first_interaction = True
                    await context.emitter.emit(
                        "first_interaction",
                        {"interaction": "resume", "thread_id": thread.get("id")},
                    )
                    await config.code.on_chat_resume(thread)
                    for step in thread.get("steps", []):
                        if "message" in step["type"]:
                            chat_context.add(Message.from_dict(step))
                    await context.emitter.resume_thread(thread)
                    return
                else:
                    logger.warning("resume: thread %s not found", context.session.thread_id_to_resume)
                    await context.emitter.send_resume_thread_error("Thread not found.")

            # Restored session with no prior interaction: just ensure on_chat_start ran.
            if context.session.restored and not context.session.has_first_interaction:
                if config.code.on_chat_start and not context.session.chat_started:
                    context.session.chat_started = True
                    task = asyncio.create_task(config.code.on_chat_start())
                    context.session.current_task = task
                return

            if config.code.on_chat_start and not context.session.chat_started:
                context.session.chat_started = True
                task = asyncio.create_task(config.code.on_chat_start())
                context.session.current_task = task
        except Exception:
            logger.error("connection_successful failed for sid=%s:\n%s", sid, _traceback.format_exc())

    cl_socket.sio.handlers["/"]["connection_successful"] = _patched_connection_successful
    logger.info("patch_sio_connect: installed")


# ── Persistence ────────────────────────────────────────────────────────────

@cl.data_layer
def data_layer():
    """SQLite-backed data layer (created once, reused).

    ONE shared conversations DB. In server mode Chainlit enforces per-user
    isolation natively: authenticate_user creates a users row per email,
    threads are stamped with the authenticated user's id, and list/get/resume
    are filtered + ownership-checked by Chainlit. In desktop/dev (no auth) the
    SQLiteDataLayer falls back to a single hardcoded local user.

    Uploaded file bytes ARE per-user: upload_dir is a UserPath that resolves
    under the current user's folder (Chainlit doesn't manage that storage), so
    attachments never mix on disk.
    """
    from app.config import USER_DATA_ROOT
    from app.data.local_storage import LocalStorageClient
    from app.data.sqlite_layer import SQLiteDataLayer
    from app.services.current_user import UserPath
    from app.services.projects import project_data_root

    # Uploads are PROJECT-scoped (attachments belong to the work they
    # were shared for); the conversations DB stays global — threads are
    # scoped by metadata tagging in the data layer instead.
    upload_dir = UserPath(lambda: project_data_root() / "uploads")
    db_path = str(USER_DATA_ROOT / "conversations.sqlite")
    storage = LocalStorageClient(upload_dir=upload_dir)
    return SQLiteDataLayer(db_path=db_path, storage_provider=storage)

# Plugins are loaded AFTER core tools so plugin-contributed ToolSpecs
# can rely on the registry being initialised. Re-import (uvicorn
# --reload) picks up manifest edits without a manual restart.
load_all()


def _compose_system_prompt(host: str | None) -> str:
    """System prompt = applicable plugins' prompts + the active project
    block (which project is live, its PROJECT.md notes, and the
    project_remember affordance). Shared by both brain paths."""
    parts: list[str] = []
    for plugin in for_host(host):
        if plugin.system_prompt:
            parts.append(plugin.system_prompt.rstrip())
    try:
        from app.services.projects import system_prompt_block

        block = system_prompt_block()
        if block:
            parts.append(block)
    except Exception:
        logger.exception("project system-prompt block failed")
    return "\n\n".join(parts)


def _apply_current_user() -> str | None:
    """Set the current-user contextvar from Chainlit's authenticated session
    user (server mode) so per-user paths AND per-user settings.json resolve
    correctly for this handler. Returns the email (None on desktop/dev).

    Called at the top of every chat entry point — on_chat_start / _resume read
    settings before on_message runs, so setting it only in on_message would
    make those earlier reads hit the shared settings file in server mode."""
    from app.services.current_user import set_current_email
    try:
        user = cl.user_session.get("user")
        email = getattr(user, "identifier", None) if user else None
    except Exception:
        email = None
    set_current_email(email)
    return email


@cl.on_chat_start
async def on_chat_start() -> None:
    """Initialise per-session state.

    Phase 1: read provider/api_key/model from the user's settings.json
    once and stash on the session. The widget posts the page's host to
    ``cl.user_session`` via ``window_message`` so plugin selection can
    happen later in the conversation.
    """
    try:
        sid = cl.context.session.id  # type: ignore[attr-defined]
    except Exception:
        sid = "?"
    logger.info("on_chat_start: sid=%s", sid)

    email = _apply_current_user()

    # A fresh pane starts a brand-new SDK session on its next turn — unless a
    # history pick is latched (the pick's select route fired just before the
    # pane remounted). Re-derive the dropdown's "active session" accordingly,
    # so a reloaded pane doesn't keep advertising a conversation it won't
    # actually continue.
    try:
        from app.services.agent_sdk.selection import peek_pending, set_active
        _sel, _picked = peek_pending(email)
        set_active(email, _picked if _sel else None)
    except Exception:
        logger.exception("on_chat_start: active-session reset failed")

    user_settings = load_user_settings()
    provider = user_settings.get("provider", "anthropic")
    api_keys = user_settings.get("api_keys") or {}
    models = user_settings.get("models") or {}
    cl.user_session.set("messages", [])
    cl.user_session.set("provider", provider)
    cl.user_session.set("api_key", resolve_api_key(provider, api_keys))
    cl.user_session.set("model", models.get(provider))
    # host is set by on_window_message which can arrive before or after
    # on_chat_start — never overwrite it here.

    # Register with the per-session metadata registry that feeds the MCP
    # debugging endpoint. user_agent is pulled from the WSGI environ on
    # the underlying WebsocketSession.
    try:
        from app.services import cl_sessions
        ws = cl.context.session  # type: ignore[attr-defined]
        env = getattr(ws, "environ", None) or {}
        ua = env.get("HTTP_USER_AGENT") if isinstance(env, dict) else None
        cl_sessions.record_chat_start(ws.id, user_agent=ua)
    except Exception:
        pass

    # The Claude (subscription) brain uses a subscription token collected
    # in-chat, not an API key — don't nag about a missing key for it.
    from app.services.agent_sdk import BRAIN_PROVIDER

    if provider == "codex" and not resolve_api_key(provider, api_keys):
        await cl.Message(
            content="⚠️ Codex is not signed in. Run `codex login` in a terminal.",
        ).send()
    elif provider not in (BRAIN_PROVIDER, "codex") and not api_keys.get(provider):
        await cl.Message(
            content="⚠️ No API key configured. Open ⚙ Settings to add one.",
        ).send()

    # Log connector + tool state at session start so we can debug
    # "why are MCP tools missing" from the log.
    try:
        import asyncio
        from app.services.mcp.registry import MCP_CONNECTORS, refresh_all
        from app.tools.registry import registry as _tool_registry
        for key, conn in MCP_CONNECTORS.items():
            logger.info(
                "on_chat_start: connector=%r status=%r owned=%r",
                key, conn.status, sorted(conn.owned_tool_names),
            )
        all_tools = list(_tool_registry._tools.keys())
        logger.info("on_chat_start: total registered tools=%d: %s", len(all_tools), all_tools)
        # Re-probe if connectors exist but none have synthesised tools.
        if MCP_CONNECTORS and not any(
            c.owned_tool_names for c in MCP_CONNECTORS.values()
        ):
            logger.info("on_chat_start: no MCP tools synthesised — triggering background refresh_all()")
            asyncio.create_task(refresh_all())
    except Exception:
        logger.exception("on_chat_start: MCP diagnostic failed")


@cl.on_chat_resume
async def on_chat_resume(thread: ThreadDict) -> None:
    """Restore session state when the user reopens an existing thread.

    The UI already shows the stored steps from the database.  Here we
    reconstruct the in-memory LLM message list so the agent has the
    conversation context for the next user turn.
    """
    _apply_current_user()
    user_settings = load_user_settings()
    provider = user_settings.get("provider", "anthropic")
    api_keys = user_settings.get("api_keys") or {}
    models = user_settings.get("models") or {}
    cl.user_session.set("provider", provider)
    cl.user_session.set("api_key", resolve_api_key(provider, api_keys))
    cl.user_session.set("model", models.get(provider))

    messages: list[LlmMessage] = []
    for step in thread.get("steps") or []:
        step_type = step.get("type")
        output = (step.get("output") or "").strip()
        if not output:
            continue
        if step_type == "user_message":
            messages.append(
                LlmMessage(role="user", content=[{"type": "text", "text": output}])
            )
        elif step_type == "assistant_message":
            messages.append(
                LlmMessage(role="assistant", content=[{"type": "text", "text": output}])
            )

    cl.user_session.set("messages", messages)
    logger.info(
        "on_chat_resume: thread=%s restored %d messages", thread.get("id"), len(messages)
    )


@cl.on_window_message
async def on_window_message(message: str) -> None:
    """The bookmarklet posts ``key:value`` window-messages after mount.

    Recognised keys: ``host`` (drives plugin host-gating), ``url``,
    ``title``. Everything is forwarded to the session registry so the
    MCP debugging endpoint can show meaningful per-session metadata.
    """
    if not isinstance(message, str):
        return
    if message.startswith("host:"):
        host_val = message[len("host:"):].strip().lower() or None
        cl.user_session.set("host", host_val)
        try:
            sid = cl.context.session.id  # type: ignore[attr-defined]
        except Exception:
            sid = None
        logger.info("window_message: host=%r session=%s", host_val, sid)
    try:
        from app.services import cl_sessions
        cl_sessions.record_window_message(cl.context.session.id, message)
    except Exception:
        pass


@cl.on_message
async def on_message(user_msg: cl.Message) -> None:
    """One user turn → agent loop → streamed Chainlit primitives."""
    # Set the current-user contextvar FIRST: the per-turn settings read below
    # (provider/api_key) and all file paths must resolve under this user in
    # server mode. asyncio.to_thread copies the context into the compute
    # sandbox, so tool file writes land in the right folder too. Reset in
    # finally to avoid bleed across turns sharing this task's context.
    from app.services.current_user import set_current_email
    email = _apply_current_user()

    try:
        await _run_message_turn(user_msg, email)
    finally:
        set_current_email(None)


async def _run_message_turn(user_msg: cl.Message, email: str | None) -> None:
    # Brain fork: the Claude Agent SDK ("claude_code") owns its own
    # conversation, tool loop, and session store, so it is intercepted here —
    # before any normalised-provider machinery — and the three API providers'
    # path below stays byte-for-byte unchanged.
    from app.services.agent_sdk import BRAIN_PROVIDER

    fresh_top = load_user_settings()
    if fresh_top.get("provider") == BRAIN_PROVIDER:
        await _run_agent_sdk_turn(user_msg, email, fresh_top)
        return

    messages: list[LlmMessage] = cl.user_session.get("messages", [])
    snapshot = len(messages)

    # Build the user-turn content blocks: any attached image elements
    # prefix the text block (Anthropic's image-after-text vs image-before-text
    # is treated the same by the model; before keeps the upload visible
    # in transcripts).
    content: list[dict[str, Any]] = []
    for el in user_msg.elements or []:
        block = _element_to_image_block(el)
        if block is not None:
            content.append(block)
    content.append({"type": "text", "text": user_msg.content})

    messages.append(LlmMessage(role="user", content=content))

    # Re-read settings from disk on every turn so changes saved via the
    # in-pane SettingsView take effect without needing the user to reset
    # the session.
    fresh = load_user_settings()
    provider = fresh.get("provider", "anthropic")
    api_keys = fresh.get("api_keys") or {}
    models = fresh.get("models") or {}
    api_key = resolve_api_key(provider, api_keys)
    if not api_key:
        await cl.Message(
            content=(
                "⚠️ Codex is not signed in. Run `codex login` in a terminal."
                if provider == "codex"
                else f"⚠️ No API key for provider {provider!r}. Open ⚙ Settings to add one."
            ),
        ).send()
        del messages[snapshot:]
        return
    cl.user_session.set("provider", provider)
    cl.user_session.set("api_key", api_key)
    cl.user_session.set("model", models.get(provider))

    # Compose the system prompt entirely from applicable plugins. The
    # default plugin (host_patterns: ["*"]) always contributes the base
    # Voitta prompt; host-specific plugins layer their addenda on top.
    host = cl.user_session.get("host")
    try:
        _sid = cl.context.session.id  # type: ignore[attr-defined]
    except Exception:
        _sid = None
    logger.info("on_message: host=%r session=%s", host, _sid)
    system = _compose_system_prompt(host)

    session_id: str | None
    try:
        session_id = cl.context.session.id  # type: ignore[attr-defined]
    except Exception:
        session_id = None

    # email is the authenticated user (server mode) or None (desktop/dev);
    # the contextvar was already set by the on_message wrapper.
    ctx = ToolCtx(session_id=session_id, host=host, email=email)

    try:
        await run_turn(
            messages=messages,
            run=RunContext(
                provider_id=provider,
                api_key=api_key,
                model=models.get(provider),
                system=system,
                tool_ctx=ctx,
                max_tokens=fresh.get("max_tokens", DEFAULT_MAX_TOKENS),
                max_tool_iterations=fresh.get(
                    "max_tool_iterations", DEFAULT_MAX_TOOL_ITERATIONS,
                ),
                sink=ChainlitSink(),
            ),
        )
        cl.user_session.set("messages", messages)
    except Exception as exc:  # surface to the user instead of crashing the socket
        logger.exception("on_message failed")
        del messages[snapshot:]
        cl.user_session.set("messages", messages)
        await cl.Message(content=f"⚠️ {type(exc).__name__}: {exc}").send()


async def _run_agent_sdk_turn(
    user_msg: cl.Message, email: str | None, settings: dict[str, Any]
) -> None:
    """Drive one turn through the Claude Agent SDK brain.

    Session continuity within a live chat is the ``agent_sdk_session_id`` stashed
    on the Chainlit session — the SDK resumes it (continue-only) so its session
    id stays stable for the history dropdown. Cross-session restore is handled by
    the dedicated SDK restore path (it sets the same session var).
    """
    from app.services.agent_sdk import (
        AgentSdkAuthError,
        AgentSdkUnavailable,
        run_agent_sdk_turn,
    )
    from app.services.agent_sdk.onboarding import handle_auth_error

    # Attachments — API-flow parity for images: each image becomes a
    # base64 content block in the user message itself (downscaled to the
    # API's limits when needed), so the model sees it instantly with no
    # tool round-trip. Non-image files (PDF, CSV, …) are persisted into
    # the project's uploads tree and handed over as file paths — the
    # engine's Read/Bash/compute take it from there.
    user_text = user_msg.content
    image_blocks: list[dict[str, Any]] = []
    if user_msg.elements:
        images = [e for e in user_msg.elements
                  if (getattr(e, "mime", None) or "").startswith("image/")]
        others = [e for e in user_msg.elements if e not in images]
        skipped: list[str] = []
        for el in images:
            block = _element_to_image_block(el)
            if block is not None:
                image_blocks.append(_shrink_image_block(block))
            else:
                skipped.append(getattr(el, "name", None) or "image")
        if others:
            attach_lines, more_skipped = _persist_attachments_for_engine(others)
            skipped += more_skipped
            if attach_lines:
                user_text = (user_text + "\n\n" if user_text else "") + "\n".join(attach_lines)
        if skipped:
            await cl.Message(
                content="ℹ️ Couldn't read attachment(s): " + ", ".join(skipped),
            ).send()

    # System prompt is composed from applicable plugins, exactly as the
    # API-provider path does.
    host = cl.user_session.get("host")
    system = _compose_system_prompt(host)

    try:
        session_id = cl.context.session.id  # type: ignore[attr-defined]
    except Exception:
        session_id = None

    ctx = ToolCtx(session_id=session_id, host=host, email=email)
    model = (settings.get("models") or {}).get("claude_code")

    # A history-dropdown pick (resume an existing session, or start fresh)
    # arrives via the select route and latches per-user; consume it here so it
    # wins over the live continuation id for this turn onward.
    from app.services.agent_sdk.selection import set_active, take_pending

    selected, picked_id = take_pending(email)
    if selected:
        cl.user_session.set("agent_sdk_session_id", picked_id)
    resume_id = cl.user_session.get("agent_sdk_session_id")

    try:
        result = await run_agent_sdk_turn(
            user_text=user_text,
            system=system,
            model=model,
            resume_session_id=resume_id,
            ctx=ctx,
            image_blocks=image_blocks or None,
        )
        if result.session_id:
            cl.user_session.set("agent_sdk_session_id", result.session_id)
            set_active(email, result.session_id)
    except AgentSdkUnavailable:
        logger.warning("agent_sdk turn: engine unavailable")
        await cl.Message(
            content="⚠️ The Claude Code engine isn't installed on this machine, "
            "so the **Claude (subscription)** brain is unavailable. Install Claude "
            "Code, or switch provider in ⚙ Settings.",
        ).send()
    except AgentSdkAuthError as exc:
        logger.info("agent_sdk turn: auth needed (%s)", exc.detail or exc)
        # Hand off to the in-chat token-onboarding flow. On success it resumes
        # the original turn; on cancel it returns without sending.
        await handle_auth_error(
            user_text=user_text,
            system=system,
            model=model,
            resume_session_id=resume_id,
            ctx=ctx,
            image_blocks=image_blocks or None,
        )
    except Exception as exc:  # surface, don't crash the socket
        logger.exception("agent_sdk turn failed")
        await cl.Message(content=f"⚠️ {type(exc).__name__}: {exc}").send()


# ----- helpers ------------------------------------------------------------


def _persist_attachments_for_engine(elements: list[Any]) -> tuple[list[str], list[str]]:
    """Persist chat attachments to the active project's uploads tree and
    return ``(prompt_lines, skipped_names)``.

    Chainlit spools incoming elements to session-temp files that may not
    outlive the session — copying into ``uploads/attachments/`` keeps the
    path valid for engine-session resumes. Every file type is passed
    through: the engine's Read tool views images/PDFs/text natively, and
    anything else is still addressable by path from Bash/compute.
    """
    import re
    import secrets
    import shutil as _shutil

    from app.services.projects import project_data_root

    dest_dir = Path(project_data_root()) / "uploads" / "attachments"
    lines: list[str] = []
    skipped: list[str] = []
    for el in elements:
        name = getattr(el, "name", None) or "attachment"
        mime = getattr(el, "mime", None) or "application/octet-stream"
        src = getattr(el, "path", None)
        content = getattr(el, "content", None)
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name)[-80:] or "attachment"
            dest = dest_dir / f"{secrets.token_hex(4)}-{safe}"
            if src:
                _shutil.copyfile(src, dest)
            elif isinstance(content, (bytes, bytearray)):
                dest.write_bytes(bytes(content))
            else:
                skipped.append(name)
                continue
            size_kb = max(1, dest.stat().st_size // 1024)
            hint = "view it with the Read tool" if (
                mime.startswith("image/")
                or mime == "application/pdf"
                or mime.startswith("text/")
            ) else "a file on disk — use Read or run_compute as appropriate"
            lines.append(f"[Attached: {dest} ({mime}, {size_kb} KB) — {hint}]")
        except Exception:
            logger.exception("could not persist attachment %r", name)
            skipped.append(name)
    return lines, skipped


# Anthropic image limits: hard-rejects >8000px or >5 MB; anything with a
# long edge >1568px is downscaled server-side anyway (and billed before
# the downscale on the API). Normalising to ≤1568px/WebP locally keeps
# every attachment comfortably inside the limits at no vision cost.
_IMG_MAX_EDGE = 1568
_IMG_MAX_BYTES = 4 * 1024 * 1024  # stay clear of the 5 MB hard limit


def _shrink_image_block(block: dict[str, Any]) -> dict[str, Any]:
    """Downscale/re-encode an image block to fit the API's limits.

    No-op for images already small enough. On any Pillow failure the
    original block is returned — an oversized image then fails loudly at
    the API rather than silently dropping the attachment."""
    try:
        src = block["source"]
        raw = base64.b64decode(src["data"])
        from io import BytesIO

        from PIL import Image

        img = Image.open(BytesIO(raw))
        long_edge = max(img.size)
        if long_edge <= _IMG_MAX_EDGE and len(raw) <= _IMG_MAX_BYTES:
            return block
        if long_edge > _IMG_MAX_EDGE:
            scale = _IMG_MAX_EDGE / long_edge
            img = img.resize(
                (max(1, round(img.width * scale)), max(1, round(img.height * scale))),
                Image.LANCZOS,
            )
        # WebP keeps screenshots crisp at a fraction of PNG size; drop
        # alpha only if WebP can't take the mode as-is.
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA" if "A" in img.mode else "RGB")
        buf = BytesIO()
        img.save(buf, format="WEBP", quality=85, method=4)
        out = buf.getvalue()
        logger.info(
            "attachment image normalised: %d KB %s → %d KB webp (%dx%d)",
            len(raw) // 1024, src.get("media_type"), len(out) // 1024, *img.size,
        )
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/webp",
                "data": base64.b64encode(out).decode("ascii"),
            },
        }
    except Exception:
        logger.exception("image shrink failed — sending original")
        return block


def _element_to_image_block(el: Any) -> dict[str, Any] | None:
    """Turn a Chainlit message element into an Anthropic image block.

    Only ``image/*`` mimes are supported here; everything else (PDFs,
    audio) is silently skipped for phase 1.
    """
    mime = getattr(el, "mime", None) or ""
    if not mime.startswith("image/"):
        return None
    path = getattr(el, "path", None)
    content = getattr(el, "content", None)

    data: bytes | None = None
    if path:
        try:
            data = Path(path).read_bytes()
        except Exception:
            logger.exception("could not read attachment at %s", path)
    elif isinstance(content, (bytes, bytearray)):
        data = bytes(content)
    elif isinstance(content, str):
        # Could be a data URI or already-encoded base64.
        if "," in content:
            content = content.split(",", 1)[1]
        try:
            data = base64.b64decode(content)
        except Exception:
            data = None
    if not data:
        return None

    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": mime,
            "data": base64.b64encode(data).decode("ascii"),
        },
    }
