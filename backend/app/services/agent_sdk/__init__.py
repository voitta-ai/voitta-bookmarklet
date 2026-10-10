"""Claude Agent SDK brain — a 4th, self-contained chat runtime.

Parallel to the three normalised API providers (anthropic/openai/gemini): the
Agent SDK owns its own conversation, tool loop, and session store, so it is not
a ``ProviderId`` — the chat layer intercepts the ``claude_code`` selector value
*before* the provider factory and routes here instead.

Public surface:

* :data:`BRAIN_PROVIDER` / :data:`BRAIN_LABEL` — selector value + display name.
* :func:`is_available` — is the Claude Code engine installed? (gating)
* :func:`run_agent_sdk_turn` / :class:`SdkRunContext` — drive one turn, emit through a ``TurnSink``.
* :func:`list_brain_sessions` / :func:`get_brain_transcript` — history dropdown.
* :class:`AgentSdkAuthError` / :class:`AgentSdkUnavailable` — control-flow errors.
"""

from __future__ import annotations

from app.services.agent_sdk.config import (
    BRAIN_LABEL,
    BRAIN_PROVIDER,
    DEFAULT_MODEL,
    is_available,
)
from app.services.agent_sdk.errors import (
    AgentSdkAuthError,
    AgentSdkError,
    AgentSdkUnavailable,
)
from app.services.agent_sdk.runtime import SdkRunContext, TurnResult, run_agent_sdk_turn
from app.services.agent_sdk.sessions import (
    get_brain_session_info,
    get_brain_transcript,
    list_brain_sessions,
)

__all__ = [
    "BRAIN_LABEL",
    "BRAIN_PROVIDER",
    "DEFAULT_MODEL",
    "SdkRunContext",
    "TurnResult",
    "AgentSdkAuthError",
    "AgentSdkError",
    "AgentSdkUnavailable",
    "is_available",
    "run_agent_sdk_turn",
    "get_brain_session_info",
    "get_brain_transcript",
    "list_brain_sessions",
]
