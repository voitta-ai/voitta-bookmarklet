"""LLM provider factory. Phase-1: Anthropic only."""

from __future__ import annotations

from typing import Literal

from app.services.llm.base import (
    ContentBlock,
    Message,
    NormalisedRequest,
    NormalisedResponse,
    Provider,
    ProviderNotConfigured,
    TextBlock,
    ToolSchema,
    ToolUseBlock,
    Usage,
)


ProviderId = Literal["anthropic", "openai", "gemini", "requesty", "codex"]


def resolve_api_key(provider_id: str, api_keys: dict[str, str]) -> str | None:
    """Credential for a provider: the saved API key, or for ``codex`` the
    ChatGPT access token the ``codex`` CLI keeps in its auth.json."""
    if provider_id == "codex":
        from app.services.llm.codex import access_token
        retval = access_token()
    else:
        retval = api_keys.get(provider_id) or None
    return retval


def get_provider(provider_id: ProviderId, api_key: str | None) -> Provider:
    if provider_id == "codex":
        # Subscription auth: re-read from the codex CLI's auth.json per request.
        from app.services.llm.codex import CodexProvider
        return CodexProvider()
    if not api_key:
        raise ProviderNotConfigured(
            provider_id,
            f"no API key for provider {provider_id!r}",
        )
    if provider_id == "anthropic":
        from app.services.llm.anthropic import AnthropicProvider
        return AnthropicProvider(api_key=api_key)
    if provider_id == "openai":
        from app.services.llm.openai import OpenAIProvider
        return OpenAIProvider(api_key=api_key)
    if provider_id == "gemini":
        from app.services.llm.gemini import GeminiProvider
        return GeminiProvider(api_key=api_key)
    if provider_id == "requesty":
        from app.services.llm.anthropic import RequestyProvider
        return RequestyProvider(api_key=api_key)
    raise ProviderNotConfigured(provider_id, f"unknown provider {provider_id!r}")


def default_model_for(provider_id: ProviderId) -> str:
    """Best-known default model for a provider.

    Delegates to :mod:`app.services.models_catalog`, which reads the live
    cache when present and otherwise the bundled snapshot — the single
    source of truth for model ids. Never triggers a network call.
    """
    from app.services import models_catalog

    default = models_catalog.default_model_for(provider_id)
    if not default:
        raise ProviderNotConfigured(provider_id, f"no default model for {provider_id!r}")
    return default


__all__ = [
    "ContentBlock",
    "Message",
    "NormalisedRequest",
    "NormalisedResponse",
    "Provider",
    "ProviderId",
    "ProviderNotConfigured",
    "TextBlock",
    "ToolSchema",
    "ToolUseBlock",
    "Usage",
    "default_model_for",
    "get_provider",
    "resolve_api_key",
]
