"""The agent's system prompt for a page — one composer for every surface.

Used by the in-app chat (both engines) and by the external-agent tool
surface on ``/mcp`` (``vb_instructions``), so an external agent works from
exactly the instructions the in-app agent gets.
"""

from __future__ import annotations

import logging

from app.plugins import for_host

logger = logging.getLogger(__name__)


def compose(host: str | None) -> str:
    """Applicable plugins' prompts + the active project block (which
    project is live, its PROJECT.md notes, and the project_remember
    affordance)."""
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
