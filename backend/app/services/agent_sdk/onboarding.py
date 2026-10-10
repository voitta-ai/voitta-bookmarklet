"""In-chat token onboarding for the Claude (subscription) brain.

When a turn fails auth (no token / expired / rejected), this flow runs:

1. Explain how to mint a token with ``claude setup-token`` (copy tailored to
   desktop vs server).
2. Collect the token over the ``prompt_claude_token`` ``call_fn`` round-trip —
   a **masked** input on the frontend whose value rides the socket ACK. It is
   never sent as a chat message, so it never lands in a Chainlit step or the
   conversation DB.
3. Store it per-user, validate it with a probe turn.
4. On success, resume the user's original turn transparently.

This keeps the brain to the defensible posture: we *accept* a user-minted token,
we do not *offer* a claude.ai login.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import chainlit as cl

from app.config import SERVER_MODE
from app.services.agent_sdk.credentials import (
    clear_token,
    store_token,
    validate_token,
)
from app.services.agent_sdk.errors import AgentSdkAuthError

if TYPE_CHECKING:
    from app.services.agent_sdk.runtime import SdkRunContext

logger = logging.getLogger(__name__)

_SETUP_DESKTOP = (
    "To use the **Claude (subscription)** brain I need a one-time access token "
    "from your Claude Pro/Max account.\n\n"
    "1. Open a terminal and run:\n"
    "   ```\n   claude setup-token\n   ```\n"
    "2. Approve in the browser; it prints a token starting with `sk-ant-oat01-…`.\n"
    "3. Paste it into the secure box that just opened. It's stored locally and "
    "never shown in chat."
)

_SETUP_SERVER = (
    "To use the **Claude (subscription)** brain I need a one-time access token "
    "from your Claude Pro/Max account.\n\n"
    "1. On **your own computer** (with Claude Code installed), run:\n"
    "   ```\n   claude setup-token\n   ```\n"
    "2. Approve in the browser; it prints a token starting with `sk-ant-oat01-…`.\n"
    "3. Paste it into the secure box that just opened. It's stored only against "
    "your account here and never shown in chat."
)


async def _prompt_for_token() -> str | None:
    """Open the masked input on the FE and return the token, or None if the
    user cancelled / the round-trip failed."""
    instructions = _SETUP_SERVER if SERVER_MODE else _SETUP_DESKTOP
    try:
        res = await cl.CopilotFunction(
            name="prompt_claude_token", args={"instructions": instructions}
        ).acall()
    except Exception:
        logger.exception("prompt_claude_token round-trip failed")
        return None
    if not isinstance(res, dict) or res.get("cancelled"):
        return None
    token = res.get("token")
    if not isinstance(token, str) or not token.strip():
        return None
    return token.strip()


async def handle_auth_error(
    *,
    user_text: str,
    run: SdkRunContext,
    image_blocks: list | None = None,
) -> None:
    """Run the onboarding flow, then resume the original turn on success."""
    # Imported here to avoid an import cycle (runtime imports nothing from us).
    from app.services.agent_sdk.runtime import run_agent_sdk_turn

    await cl.Message(content=_SETUP_SERVER if SERVER_MODE else _SETUP_DESKTOP).send()

    token = await _prompt_for_token()
    if not token:
        await cl.Message(
            content="Token entry cancelled — the Claude (subscription) brain "
            "needs a token to run. Send another message to try again, or switch "
            "provider in ⚙ Settings.",
        ).send()
        return

    store_token(token)
    probe = cl.Message(content="⏳ Validating your Claude token…")
    await probe.send()
    try:
        verdict = await validate_token()
    except Exception:
        # Belt and braces: the probe owns its own failure modes, but an escaping
        # exception here would strand the "⏳ Validating…" message and skip the
        # resume below — the one outcome onboarding must never produce.
        logger.exception("token probe raised; treating as inconclusive")
        verdict = "inconclusive"
    if verdict == "auth_failed":
        clear_token()
        probe.content = (
            "⚠️ That token didn't authenticate. It may be mistyped or expired — "
            "run `claude setup-token` again and retry."
        )
        await probe.update()
        return
    if verdict == "inconclusive":
        # Keep the token: the probe reached no verdict (slow engine start, no
        # result message), and a token that got this far is usually good. The
        # resume below raises AgentSdkAuthError and re-prompts if it isn't.
        probe.content = (
            "⚠️ Couldn't confirm the token in time — saved it and carrying on. "
            "If it turns out to be wrong you'll be asked for a new one."
        )
    else:
        probe.content = "✅ Claude subscription connected."
    await probe.update()

    # Resume the original turn now that we're authenticated.
    try:
        result = await run_agent_sdk_turn(
            user_text=user_text, run=run, image_blocks=image_blocks,
        )
        if result.session_id:
            cl.user_session.set("agent_sdk_session_id", result.session_id)
            from app.services.agent_sdk.selection import set_active
            set_active(run.tool_ctx.email, result.session_id)
    except AgentSdkAuthError:
        await cl.Message(
            content="⚠️ Still not authenticated after saving the token. "
            "Please re-run `claude setup-token` and try again.",
        ).send()
    except Exception as exc:  # noqa: BLE001
        logger.exception("resume after onboarding failed")
        await cl.Message(content=f"⚠️ {type(exc).__name__}: {exc}").send()
