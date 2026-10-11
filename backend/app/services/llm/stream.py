"""Normalised streaming event types.

Provider adapters yield these from their `stream()` async context manager.
The chat orchestrator consumes them and emits SSE events. See
streaming-migration.md §3.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Union

from app.services.llm.base import StopReason, Usage


@dataclass
class BlockStart:
    block_index: int
    kind: Literal["text", "tool_use"]
    tool_id: str | None = None
    tool_name: str | None = None


@dataclass
class BlockDelta:
    block_index: int
    kind: Literal["text", "tool_args"]
    text: str


@dataclass
class BlockStop:
    block_index: int


@dataclass
class MessageStop:
    stop_reason: StopReason
    usage: Usage
    # The provider's own stop/finish reason before normalisation (e.g.
    # Anthropic "refusal", OpenAI "content_filter", Gemini "SAFETY"), and any
    # error code/message the provider attached. stop_reason above folds these
    # into "end_turn"; consumers that must tell a provider-side block apart
    # (the eval API, voitta-bookmarklet#25) read these instead.
    provider_reason: str | None = None
    provider_message: str | None = None


@dataclass
class StreamError:
    type: str
    message: str
    partial: bool


StreamEvent = Union[BlockStart, BlockDelta, BlockStop, MessageStop, StreamError]
