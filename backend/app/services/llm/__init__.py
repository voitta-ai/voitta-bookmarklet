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


ProviderId = Literal["anthropic", "openai", "gemini", "requesty"]


def get_provider(provider_id: ProviderId, api_key: str | None) -> Provider:
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
]
