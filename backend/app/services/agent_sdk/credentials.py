"""Per-user storage and validation of the Claude subscription OAuth token.

The token (``sk-ant-oat01-…``, minted by ``claude setup-token``) is a long-lived
subscription credential. It is stored **only** in a 0600 file under the user's
``CLAUDE_CONFIG_DIR`` and injected into the engine subprocess as
``CLAUDE_CODE_OAUTH_TOKEN`` (see :func:`app.services.agent_sdk.config.subprocess_env`).
It is never written to a chat message, a Chainlit step, or the conversation DB —
it arrives over the ``call_fn`` ACK and lands here directly.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Literal

from app.services.agent_sdk.config import (
    config_dir,
    engine_cli_path,
    subprocess_env,
    workspace_dir,
)

logger = logging.getLogger(__name__)

_TOKEN_FILENAME = "voitta_oauth_token"

# How long the validation probe waits for the engine to answer. Generous enough
# for a cold engine start, but bounded: an unbounded probe leaves onboarding's
# "⏳ Validating…" message stuck forever and never resumes the user's turn.
# Override with VOITTA_BRAIN_PROBE_TIMEOUT_S (seconds).
try:
    _PROBE_TIMEOUT_S = float(os.environ.get("VOITTA_BRAIN_PROBE_TIMEOUT_S", "75"))
except ValueError:
    _PROBE_TIMEOUT_S = 75.0

ProbeResult = Literal["ok", "auth_failed", "inconclusive"]


def _token_path() -> Path:
    return config_dir() / _TOKEN_FILENAME


def has_token() -> bool:
    p = _token_path()
    return p.exists() and bool(p.read_text(encoding="ascii").strip())


def load_token() -> str | None:
    try:
        p = _token_path()
        if not p.exists():
            return None
        val = p.read_text(encoding="ascii").strip()
        return val or None
    except Exception:
        logger.exception("failed to read agent-sdk token")
        return None


def store_token(token: str) -> None:
    p = _token_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(token.strip(), encoding="ascii")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def clear_token() -> None:
    try:
        _token_path().unlink(missing_ok=True)
    except Exception:
        logger.exception("failed to clear agent-sdk token")


async def validate_token() -> ProbeResult:
    """Spawn a minimal engine turn to confirm the stored token authenticates.

    Three outcomes, because "couldn't tell" is not the same as "it's bad":

    * ``"ok"``           — the engine completed a turn with this token.
    * ``"auth_failed"``  — the engine rejected it; the caller should clear the
      token and re-prompt.
    * ``"inconclusive"`` — no verdict: the probe timed out, the engine binary is
      missing, or the stream ended without a ``ResultMessage``. The token is
      most likely fine, so the caller must **not** clear it — the real turn that
      follows raises ``AgentSdkAuthError`` and re-prompts if it truly is bad.
    """
    if not has_token():
        return "auth_failed"

    # Imported lazily (the SDK is a userbase install) and inside the try below,
    # so a broken import degrades to "inconclusive" rather than escaping into
    # the caller mid-onboarding and freezing its "⏳ Validating…" message.
    try:
        from claude_agent_sdk import (
            ClaudeAgentOptions,
            PermissionResultDeny,
            ResultMessage,
            query,
        )
        from claude_agent_sdk import CLINotFoundError  # type: ignore

        from app.services.agent_sdk.runtime import (
            _AUTH_HINTS,
            _is_auth_failure,
            user_prompt_stream,
        )
    except Exception:
        logger.exception("token probe could not load the agent SDK")
        return "inconclusive"

    async def _deny_all(tool_name: str, _tool_input: dict, _ctx) -> object:
        """The probe is a one-shot "reply ok" turn with no allowed tools.

        It must never reach the interactive permission flow — there is no chat
        context behind it, so a prompt there would block until the timeout.
        """
        return PermissionResultDeny(message="tools are unavailable during token validation")

    options = ClaudeAgentOptions(
        cli_path=await asyncio.to_thread(engine_cli_path),
        cwd=str(workspace_dir()),
        env=subprocess_env(),
        allowed_tools=[],
        can_use_tool=_deny_all,
        setting_sources=[],
        max_turns=1,
        system_prompt="Reply with the single word: ok",
    )
    # Held explicitly so the engine subprocess can be torn down on every exit
    # path — on timeout it is otherwise left running behind the abandoned probe.
    agen = query(prompt=user_prompt_stream("ping"), options=options)
    try:
        async with asyncio.timeout(_PROBE_TIMEOUT_S):
            async for message in agen:
                if isinstance(message, ResultMessage):
                    if message.is_error:
                        return "auth_failed" if _is_auth_failure(message) else "inconclusive"
                    return "ok"
    except TimeoutError:
        logger.warning(
            "token probe did not complete within %.0fs — inconclusive, keeping token",
            _PROBE_TIMEOUT_S,
        )
        return "inconclusive"
    except CLINotFoundError:
        # No engine binary at all — says nothing about the token.
        return "inconclusive"
    except Exception as exc:  # noqa: BLE001
        logger.info("token probe failed: %s", exc)
        if any(h in str(exc).lower() for h in _AUTH_HINTS):
            return "auth_failed"
        return "inconclusive"
    finally:
        # Bounded: aclose() waits on the engine shutting down, and a wedged
        # engine must not re-introduce the hang this timeout exists to prevent.
        try:
            await asyncio.wait_for(agen.aclose(), 10.0)
        except Exception:
            pass
    # Stream ended without a ResultMessage — no verdict either way.
    return "inconclusive"
