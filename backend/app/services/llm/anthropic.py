"""Anthropic adapter.

The normalised types already use Anthropic's block shape, so this is
mostly a pass-through. We pin the official endpoint explicitly because
the process may be launched from a shell where ``ANTHROPIC_BASE_URL`` is
set to a proxy (e.g. Claude Code's local relay), which would reject
user-supplied API keys with a 401.

Defence in depth:

  1. ``ANTHROPIC_BASE_URL`` is popped from ``os.environ`` at import time
     so no code path on the server side can read it.
  2. ``base_url=ANTHROPIC_BASE_URL`` is still passed explicitly to the
     client constructor — the SDK respects this regardless of env state.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from anthropic import AsyncAnthropic

from app.services.llm.base import (
    BaseProvider,
    Message,
    NormalisedRequest,
    Usage,
)
from app.services.llm.stream import (
    BlockDelta,
    BlockStart,
    BlockStop,
    MessageStop,
    StreamError,
    StreamEvent,
)


logger = logging.getLogger(__name__)


# Hardcoded to the official endpoint. Do not parameterise — this is the
# only URL the backend should ever talk to for Anthropic.
ANTHROPIC_BASE_URL = "https://api.anthropic.com"


# Strip Anthropic env-var overrides at import time. Idempotent: each
# `pop` returns None if the var wasn't set, no-op then.
for _var in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
    _stale = os.environ.pop(_var, None)
    if _stale is not None:
        # Surface this so it's obvious in the logs why connections might
        # have been failing under e.g. Claude Code's local relay.
        logger.info(
            "anthropic adapter: stripped %s from os.environ at import "
            "(was %r); using hardcoded base_url=%s",
            _var,
            (_stale[:24] + "…") if len(_stale) > 24 else _stale,
            ANTHROPIC_BASE_URL,
        )


class AnthropicProvider(BaseProvider):
    id = "anthropic"

    def __init__(self, api_key: str) -> None:
        self._client = AsyncAnthropic(api_key=api_key, base_url=ANTHROPIC_BASE_URL)

    async def list_models(self) -> list[str]:
        """Live Anthropic catalog, newest-first.

        Filter: ids starting ``claude-`` (drops any non-chat SKUs the API
        might list). The SDK returns newest-first already; we additionally
        sort by ``created_at`` when present to be robust.
        """
        collected: list[tuple[Any, str]] = []
        async for model in self._client.models.list(limit=1000):
            mid = getattr(model, "id", None)
            if not isinstance(mid, str) or not mid.startswith("claude-"):
                continue
            created = getattr(model, "created_at", None)
            collected.append((created, mid))
        # Sort newest-first; entries without a timestamp keep API order at the end.
        collected.sort(key=lambda t: (t[0] is not None, str(t[0])), reverse=True)
        return [mid for _, mid in collected]

    def _build_kwargs(self, req: NormalisedRequest) -> dict[str, Any]:
        messages = [_message_to_dict(m) for m in req.messages]

        # Prompt-cache breakpoints — Claude Code strategy.
        #
        # The Messages API renders the request as:  tools → system → messages.
        # A single ``cache_control: ephemeral`` marker on a content block
        # tells the server to cache the entire prefix UP TO that point.
        # Two markers is enough:
        #
        #   1) The LAST system block — caches tools + system together.
        #      Tools are static across turns; system text is static
        #      (plugin addenda only change when the bookmarklet host
        #      changes, which is a new conversation anyway). One marker
        #      here recovers most of the cache benefit.
        #
        #   2) The LAST message's last block — caches the entire
        #      conversation up to and including the most recent turn.
        #      Rebuilt fresh each turn at the new tail position;
        #      stability comes from PREFIX INVARIANCE (we never mutate
        #      prior messages), so the previous turn's marker is still a
        #      cache entry on the server and the new (longer) marker
        #      extends it.
        #
        # We DELIBERATELY do not place markers on the second/third-to-last
        # messages. Anthropic's KV-page manager evicts cached positions
        # that aren't on a stable boundary; redundant markers fragment
        # the prefix without recovering more tokens than the single tail
        # marker already does. See voitta-rag indexed Claude Code source
        # (claude.ts::addCacheBreakpoints) for the upstream pattern.
        if messages:
            last_blocks = messages[-1].get("content")
            if last_blocks and isinstance(last_blocks[-1], dict):
                last_blocks[-1]["cache_control"] = {"type": "ephemeral"}

        kwargs: dict[str, Any] = dict(
            model=req.model,
            system=[
                {
                    "type": "text",
                    "text": req.system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            max_tokens=req.max_tokens,
            messages=messages,
        )
        if req.tools:
            # `eager_input_streaming: true` opts each tool into Anthropic's
            # fine-grained tool streaming. Without this flag the server
            # buffers tool args for schema validation and ships them as
            # a single late burst (observed ~30s gap mid-block for large
            # JSON inputs); with it, partial_json fragments stream as the
            # model generates them. Trade-off: mid-stream JSON may be
            # invalid — we already only parse on BlockStop, and fall back
            # to {"_raw": joined} if json.loads fails (chat.py).
            kwargs["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.input_schema,
                    "eager_input_streaming": True,
                }
                for t in req.tools
            ]
        return kwargs

    def stream(self, req: NormalisedRequest):
        return _AnthropicStreamCM(self._client, self._build_kwargs(req))


# Public Requesty router. Private deployments (same API, different host)
# set REQUESTY_BASE_URL instead — keep such hostnames out of this repo.
REQUESTY_BASE_URL = "https://router.requesty.ai"


class RequestyProvider(AnthropicProvider):
    """Requesty router via its Anthropic-compatible Messages endpoint.

    Same wire as Anthropic, so streaming, tool use, inline images and
    ``cache_control`` pass straight through; Requesty translates for
    non-Anthropic models. Model ids are router-qualified
    (``anthropic/claude-sonnet-5``, ``openai/gpt-5.5``, ``vertex/…``).
    """

    id = "requesty"

    def __init__(self, api_key: str) -> None:
        base_url = os.environ.get("REQUESTY_BASE_URL") or REQUESTY_BASE_URL
        # The SDK appends /v1/messages itself; tolerate the OpenAI-style
        # ".../v1" form other tools use for the same router.
        base_url = base_url.rstrip("/").removesuffix("/v1")
        self._client = AsyncAnthropic(api_key=api_key, base_url=base_url)

    async def list_models(self) -> list[str]:
        """Router catalog, newest-first.

        ``/v1/models`` here is OpenAI-shaped (``{object, data}``), not
        Anthropic's paginated shape, so read it raw.
        """
        body = await self._client.get("/v1/models", cast_to=object)
        models = [m for m in (body or {}).get("data") or [] if isinstance(m.get("id"), str)]
        models.sort(key=lambda m: m.get("created") or 0, reverse=True)
        retval = [m["id"] for m in models]
        return retval


class _AnthropicStreamCM:
    """Async context manager wrapping ``client.messages.create(stream=True)``.

    Maps Anthropic SSE events to our normalised ``StreamEvent``s.

    We deliberately use the raw streaming API (``.create(stream=True)``)
    rather than the higher-level ``.stream()`` helper. The helper
    accumulates each tool_use's partial JSON buffer on every delta and
    calls ``jiter.from_json(buf, partial_mode=True)`` — which raises
    ``ValueError: expected value at line 1 column N`` whenever the
    model's tool args briefly contain non-JSON characters
    (e.g. ``{"folder": x`` before the value start lands). That blows
    up the entire async-for iteration mid-message even though we don't
    need the SDK's accumulation in the first place — chat.py already
    accumulates ``args_buf`` itself and parses on ``content_block_stop``.
    Going through the raw iterator sidesteps the failure mode entirely.
    """

    def __init__(self, client: AsyncAnthropic, kwargs: dict[str, Any]) -> None:
        self._client = client
        self._kwargs = kwargs
        self._stream_obj = None

    async def __aenter__(self) -> AsyncIterator[StreamEvent]:
        # ``stream=True`` makes ``create`` return an ``AsyncStream`` of raw
        # ``RawMessageStreamEvent``s, no auto-accumulate, no partial-json
        # parse. We close the stream explicitly in __aexit__.
        self._stream_obj = await self._client.messages.create(
            stream=True, **self._kwargs
        )
        return self._iter_events(self._stream_obj)

    async def __aexit__(self, exc_type, exc, tb):
        if self._stream_obj is not None:
            try:
                await self._stream_obj.close()
            except Exception:
                pass
        return False

    @staticmethod
    async def _iter_events(stream_obj: Any) -> AsyncIterator[StreamEvent]:
        usage = Usage()
        stop_reason: str = "end_turn"

        # The Anthropic SDK's stream helper accumulates each tool_use's
        # ``partial_json`` buffer and calls ``pydantic_core.from_json(...,
        # partial_mode=True)`` on every delta. When the model
        # occasionally emits a tool_use whose first bytes aren't valid
        # JSON (rare but observed — usually a hallucinated leading
        # character before the ``{``), ``from_json`` raises
        # ``ValueError`` and the entire stream iteration explodes
        # mid-message. We drive the iterator manually so we can catch
        # that ValueError per-event, surface a StreamError, and stop
        # cleanly — the chat route already terminates the SSE
        # gracefully on this signal.
        iterator = stream_obj.__aiter__()
        while True:
            try:
                event = await iterator.__anext__()
            except StopAsyncIteration:
                break
            except ValueError as exc:
                logger.warning(
                    "anthropic stream accumulation failed: %s", exc
                )
                yield StreamError(
                    type="malformed_tool_args",
                    message=(
                        "Anthropic SDK could not parse tool input JSON "
                        f"mid-stream: {exc}. Retry the request."
                    ),
                    partial=True,
                )
                yield MessageStop(stop_reason="end_turn", usage=usage)  # type: ignore[arg-type]
                return
            etype = getattr(event, "type", None)
            if etype == "content_block_start":
                idx = getattr(event, "index", 0)
                block = getattr(event, "content_block", None)
                btype = getattr(block, "type", None) if block is not None else None
                if btype == "text":
                    yield BlockStart(block_index=idx, kind="text")
                elif btype == "tool_use":
                    yield BlockStart(
                        block_index=idx,
                        kind="tool_use",
                        tool_id=getattr(block, "id", "") or "",
                        tool_name=getattr(block, "name", "") or "",
                    )
                # `thinking` blocks are not surfaced to the orchestrator as
                # streamable blocks — they're internal to Claude's reasoning
                # and we keep the current behaviour of ignoring them on the
                # wire. They still arrive in the final message via the
                # MessageStop path below if the caller needs them.
            elif etype == "content_block_delta":
                idx = getattr(event, "index", 0)
                delta = getattr(event, "delta", None)
                dtype = getattr(delta, "type", None) if delta is not None else None
                if dtype == "text_delta":
                    text = getattr(delta, "text", "") or ""
                    if text:
                        yield BlockDelta(block_index=idx, kind="text", text=text)
                elif dtype == "input_json_delta":
                    partial = getattr(delta, "partial_json", "") or ""
                    if partial:
                        yield BlockDelta(block_index=idx, kind="tool_args", text=partial)
            elif etype == "content_block_stop":
                idx = getattr(event, "index", 0)
                yield BlockStop(block_index=idx)
            elif etype == "message_delta":
                delta = getattr(event, "delta", None)
                if delta is not None:
                    sr = getattr(delta, "stop_reason", None)
                    if sr:
                        stop_reason = sr
                u = getattr(event, "usage", None)
                if u is not None:
                    # `message_delta` usage is cumulative output_tokens.
                    out = getattr(u, "output_tokens", None)
                    if out is not None:
                        usage.output_tokens = out
            elif etype == "message_start":
                msg = getattr(event, "message", None)
                if msg is not None:
                    u = getattr(msg, "usage", None)
                    if u is not None:
                        usage.input_tokens = getattr(u, "input_tokens", 0) or 0
                        usage.output_tokens = getattr(u, "output_tokens", 0) or 0
                        usage.cache_read_input_tokens = (
                            getattr(u, "cache_read_input_tokens", 0) or 0
                        )
                        usage.cache_creation_input_tokens = (
                            getattr(u, "cache_creation_input_tokens", 0) or 0
                        )
            elif etype == "message_stop":
                if stop_reason not in ("end_turn", "tool_use", "max_tokens", "stop_sequence"):
                    stop_reason = "end_turn"
                yield MessageStop(stop_reason=stop_reason, usage=usage)  # type: ignore[arg-type]


def _message_to_dict(m: Message) -> dict[str, Any]:
    return {"role": m.role, "content": [_strip_internal_keys(b) for b in m.content]}


def _strip_internal_keys(block: dict[str, Any]) -> dict[str, Any]:
    """Drop leading-underscore keys before sending to Anthropic.

    The chat orchestrator tags ``tool_result`` blocks with ``_name`` (used by
    the Gemini adapter to populate ``function_response.name``). Anthropic's
    Messages API rejects unknown fields with HTTP 400. Convention: any
    cross-provider sentinel field starts with ``_``; this filter keeps the
    Anthropic wire payload clean without coupling the adapters together.
    """

    return {k: v for k, v in block.items() if not (isinstance(k, str) and k.startswith("_"))}


