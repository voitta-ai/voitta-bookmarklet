"""OpenAI adapter — uses the Responses API.

Mapping rules (in detail in docs/03-providers.md):

  request:
    system            -> instructions
    messages          -> input  (Anthropic blocks → Responses items)
    tools             -> [{type: "function", name, description, parameters}]

  response:
    output_text       -> {type: "text", text}
    function_call     -> {type: "tool_use", id, name, input}

We treat the OpenAI ``call_id`` as the Anthropic ``tool_use.id``, which is
the only stable cross-provider id we have.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

from openai import AsyncOpenAI

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
    StreamEvent,
)


# Substrings that mark a `gpt-*`/`o*` id as NOT a chat model. Keeps the
# filter permissive (new gpt/o releases pass automatically) while dropping
# the non-chat SKUs that share those prefixes.
_OPENAI_NON_CHAT_MARKERS = (
    "embedding",
    "tts",
    "whisper",
    "audio",
    "transcribe",
    "realtime",
    "image",
    "dall-e",
    "moderation",
    "search",
    "computer-use",
)


def _is_openai_chat_model(mid: str) -> bool:
    low = mid.lower()
    if not (low.startswith("gpt-") or low.startswith("o1") or low.startswith("o3")
            or low.startswith("o4") or low.startswith("chatgpt")):
        return False
    return not any(marker in low for marker in _OPENAI_NON_CHAT_MARKERS)


class OpenAIProvider(BaseProvider):
    id = "openai"

    def __init__(self, api_key: str) -> None:
        self._client = AsyncOpenAI(api_key=api_key)

    async def list_models(self) -> list[str]:
        """Live OpenAI catalog, chat-capable only, newest-first.

        The models endpoint returns everything — embeddings, tts, whisper,
        dall-e, moderation, realtime, etc. Keep only chat-completion SKUs:
        the ``gpt-*`` and ``o<n>`` (reasoning) families, minus the known
        non-chat variants that still match those prefixes.
        """
        collected: list[tuple[int, str]] = []
        async for model in self._client.models.list():
            mid = getattr(model, "id", None)
            if not isinstance(mid, str) or not _is_openai_chat_model(mid):
                continue
            created = getattr(model, "created", 0) or 0
            collected.append((int(created), mid))
        collected.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return [mid for _, mid in collected]

    def _build_kwargs(self, req: NormalisedRequest) -> dict[str, Any]:
        kwargs: dict[str, Any] = dict(
            model=req.model,
            instructions=req.system,
            max_output_tokens=req.max_tokens,
            input=_messages_to_responses_input(req.messages),
        )
        if req.tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                }
                for t in req.tools
            ]
        return kwargs

    def stream(self, req: NormalisedRequest):
        return _OpenAIStreamCM(self._client, self._build_kwargs(req))


class _OpenAIStreamCM:
    """Async context manager for the Responses API streaming endpoint.

    Responses API streaming uses semantic events:
      - response.output_item.added         (a new item begins; for function_call
                                            items, this carries name + call_id)
      - response.output_text.delta         (text fragment for the current message)
      - response.function_call_arguments.delta  (JSON arg fragment)
      - response.output_item.done          (close of the current item)
      - response.completed                 (terminal; carries final usage)

    Block-index assignment is local to this adapter (`next_block_index`),
    incremented for each new item (text message or function_call).
    """

    def __init__(self, client: AsyncOpenAI, kwargs: dict[str, Any]) -> None:
        self._client = client
        self._kwargs = kwargs
        self._cm = None
        self._stream_obj = None

    async def __aenter__(self) -> AsyncIterator[StreamEvent]:
        self._cm = self._client.responses.stream(**self._kwargs)
        self._stream_obj = await self._cm.__aenter__()
        return self._iter_events(self._stream_obj)

    async def __aexit__(self, exc_type, exc, tb):
        if self._cm is not None:
            return await self._cm.__aexit__(exc_type, exc, tb)
        return False

    @staticmethod
    async def _iter_events(stream_obj: Any) -> AsyncIterator[StreamEvent]:
        # Map OpenAI item index → our block_index.
        idx_map: dict[int, int] = {}
        # And track block kind so block_stop emits cleanly.
        kind_map: dict[int, str] = {}
        next_block_index = 0
        usage = Usage()
        stop_reason: str = "end_turn"
        had_function_call = False

        async for event in stream_obj:
            etype = getattr(event, "type", None)

            if etype == "response.output_item.added":
                item = getattr(event, "item", None)
                output_index = getattr(event, "output_index", 0)
                itype = getattr(item, "type", None) if item is not None else None
                bi = next_block_index
                next_block_index += 1
                idx_map[output_index] = bi
                if itype == "function_call":
                    kind_map[bi] = "tool_use"
                    had_function_call = True
                    yield BlockStart(
                        block_index=bi,
                        kind="tool_use",
                        tool_id=getattr(item, "call_id", "") or getattr(item, "id", "") or "",
                        tool_name=getattr(item, "name", "") or "",
                    )
                elif itype == "message":
                    kind_map[bi] = "text"
                    yield BlockStart(block_index=bi, kind="text")

            elif etype == "response.output_text.delta":
                output_index = getattr(event, "output_index", 0)
                bi = idx_map.get(output_index)
                if bi is None:
                    continue
                text = getattr(event, "delta", "") or ""
                if text:
                    yield BlockDelta(block_index=bi, kind="text", text=text)

            elif etype == "response.function_call_arguments.delta":
                output_index = getattr(event, "output_index", 0)
                bi = idx_map.get(output_index)
                if bi is None:
                    continue
                frag = getattr(event, "delta", "") or ""
                if frag:
                    yield BlockDelta(block_index=bi, kind="tool_args", text=frag)

            elif etype == "response.output_item.done":
                output_index = getattr(event, "output_index", 0)
                bi = idx_map.get(output_index)
                if bi is None:
                    continue
                yield BlockStop(block_index=bi)

            elif etype == "response.completed":
                resp = getattr(event, "response", None)
                if resp is not None:
                    u = getattr(resp, "usage", None)
                    if u is not None:
                        usage.input_tokens = getattr(u, "input_tokens", 0) or 0
                        usage.output_tokens = getattr(u, "output_tokens", 0) or 0
                        details = getattr(u, "input_tokens_details", None)
                        if details is not None:
                            usage.cache_read_input_tokens = (
                                getattr(details, "cached_tokens", 0) or 0
                            )
                    incomplete = getattr(resp, "incomplete_details", None)
                    reason = getattr(incomplete, "reason", None) if incomplete else None
                    if had_function_call:
                        stop_reason = "tool_use"
                    elif reason == "max_output_tokens":
                        stop_reason = "max_tokens"
                    else:
                        stop_reason = "end_turn"
                yield MessageStop(stop_reason=stop_reason, usage=usage,  # type: ignore[arg-type]
                                  provider_reason=reason)

            elif etype in ("response.incomplete", "response.failed"):
                # Terminal events for a response the provider cut short or
                # failed: "content_filter" arrives as incomplete_details, a
                # failure as response.error. stop_reason stays "end_turn",
                # which is what the loop assumed when these events went
                # unhandled, so the chat behaves exactly as before; the raw
                # reason is passed on for consumers that classify it.
                resp = getattr(event, "response", None)
                incomplete = getattr(resp, "incomplete_details", None)
                error = getattr(resp, "error", None)
                if etype == "response.failed":
                    reason = f"failed:{getattr(error, 'code', None) or 'unknown'}"
                else:
                    reason = getattr(incomplete, "reason", None) or "incomplete"
                yield MessageStop(stop_reason="end_turn", usage=usage,  # type: ignore[arg-type]
                                  provider_reason=reason,
                                  provider_message=getattr(error, "message", None))


def _messages_to_responses_input(messages: list[Message]) -> list[dict[str, Any]]:
    """Translate Anthropic-shaped messages to Responses API input items.

    Each user/assistant message produces one or more items: text → message
    items, tool_use → function_call items (assistant only), tool_result →
    function_call_output items (user only).
    """

    out: list[dict[str, Any]] = []
    for m in messages:
        for block in m.content:
            btype = block.get("type")
            if btype == "text":
                out.append(
                    {
                        "role": m.role,
                        "type": "message",
                        "content": [{"type": "input_text", "text": block.get("text", "")}]
                        if m.role == "user"
                        else [{"type": "output_text", "text": block.get("text", "")}],
                    }
                )
            elif btype == "tool_use" and m.role == "assistant":
                out.append(
                    {
                        "type": "function_call",
                        "call_id": block["id"],
                        "name": block["name"],
                        "arguments": json.dumps(block.get("input") or {}, default=str),
                    }
                )
            elif btype == "image" and m.role == "user":
                src = block.get("source") or {}
                media = src.get("media_type", "image/png")
                data = src.get("data", "")
                out.append(
                    {
                        "role": "user",
                        "type": "message",
                        "content": [
                            {
                                "type": "input_image",
                                "image_url": f"data:{media};base64,{data}",
                            }
                        ],
                    }
                )
            elif btype == "tool_result" and m.role == "user":
                content = block.get("content", "")
                if not isinstance(content, str):
                    content = json.dumps(content, default=str)
                out.append(
                    {
                        "type": "function_call_output",
                        "call_id": block["tool_use_id"],
                        "output": content,
                    }
                )
    return out


