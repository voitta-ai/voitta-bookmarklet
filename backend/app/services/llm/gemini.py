"""Gemini adapter — uses the `google-genai` SDK.

Mapping rules (in detail in docs/03-providers.md):

  request:
    system            -> system_instruction
    messages          -> contents   (Anthropic blocks → Parts)
    tools             -> [{function_declarations: [...]}]

  response:
    candidates[0].content.parts[*].text         -> {type: "text", text}
    candidates[0].content.parts[*].functionCall -> {type: "tool_use", id, name, input}

Tool ids are minted client-side because Gemini doesn't return one;
``id = "gemini_call_<seq>_<ts>"``. Subsequent ``tool_result`` blocks
must echo this id back.

Schema sanitisation: Gemini rejects several JSON-Schema keywords that
Anthropic accepts. We strip them — see ``_sanitize_schema``. The reference
is ``the original plugin/lib/providers.js::sanitizeGeminiSchema``.
"""

from __future__ import annotations

import json
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from google import genai
from google.genai import types as genai_types

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


def _gemini_version_key(mid: str) -> tuple[float, str]:
    """Sort key extracting the leading ``gemini-<major>.<minor>`` version.

    Returns ``(version_float, id)`` so a reverse sort puts newer families
    first and orders ties alphabetically-descending (stable enough for a
    dropdown). Unparseable ids sort to the bottom.
    """
    import re

    m = re.search(r"(\d+)\.(\d+)", mid)
    ver = float(f"{m.group(1)}.{m.group(2)}") if m else 0.0
    return (ver, mid)


class GeminiProvider(BaseProvider):
    id = "gemini"

    def __init__(self, api_key: str) -> None:
        self._client = genai.Client(api_key=api_key)

    async def list_models(self) -> list[str]:
        """Live Gemini catalog, generateContent-capable only.

        Keep models whose supported actions include ``generateContent``
        (drops embedding / aqa / imagen). Strip the ``models/`` name prefix
        so ids match what the rest of the app uses. Preview/exp variants are
        kept — they're the only way to reach the newest models. Ordered
        newest-first by version number heuristic (2.5 > 2.0 > 1.5).
        """
        ids: list[str] = []
        pager = await self._client.aio.models.list()
        async for model in pager:
            actions = getattr(model, "supported_actions", None) or []
            if "generateContent" not in actions:
                continue
            name = getattr(model, "name", None)
            if not isinstance(name, str) or not name:
                continue
            ids.append(name.split("/", 1)[-1] if name.startswith("models/") else name)
        # Newest-first: sort by the leading version number descending, stable.
        ids.sort(key=_gemini_version_key, reverse=True)
        return ids

    def _build_request(self, req: NormalisedRequest) -> dict[str, Any]:
        contents = _messages_to_contents(req.messages)
        config = genai_types.GenerateContentConfig(
            system_instruction=req.system,
            max_output_tokens=req.max_tokens,
        )
        if req.tools:
            config.tools = [
                genai_types.Tool(
                    function_declarations=[
                        genai_types.FunctionDeclaration(
                            name=t.name,
                            description=t.description,
                            parameters=_sanitize_schema(t.input_schema),
                        )
                        for t in req.tools
                    ]
                )
            ]
            config.tool_config = genai_types.ToolConfig(
                function_calling_config=genai_types.FunctionCallingConfig(mode="AUTO")
            )
        return {"model": req.model, "contents": contents, "config": config}

    def stream(self, req: NormalisedRequest):
        return _GeminiStreamCM(self._client, self._build_request(req))


class _GeminiStreamCM:
    """Async context manager for `generate_content_stream`.

    Walks `candidates[0].content.parts[]` per chunk. Consecutive text Parts
    coalesce into one logical text block until a function_call Part
    interrupts. function_call Parts arrive complete in a single chunk
    (verified empirically 2026-05-14, see /tmp/gemini_stream_probe.py), so
    we emit BlockStart + BlockDelta(tool_args) + BlockStop back-to-back.
    """

    def __init__(self, client: genai.Client, request: dict[str, Any]) -> None:
        self._client = client
        self._request = request
        self._stream_obj = None

    async def __aenter__(self) -> AsyncIterator[StreamEvent]:
        self._stream_obj = await self._client.aio.models.generate_content_stream(
            **self._request
        )
        return self._iter_events(self._stream_obj)

    async def __aexit__(self, exc_type, exc, tb):
        # google-genai's stream object doesn't expose an explicit close;
        # the underlying transport is GC'd. If a closer becomes available
        # in a future SDK version, wire it here.
        self._stream_obj = None
        return False

    @staticmethod
    async def _iter_events(stream_obj: Any) -> AsyncIterator[StreamEvent]:
        next_block_index = 0
        open_text_bi: int | None = None
        had_tool = False
        usage = Usage()
        finish_str = ""
        # Raw provider reason, for consumers that classify blocks (#25): the
        # candidate's finish reason name, or "prompt:<reason>" when Gemini
        # blocked the prompt itself and returned no candidate.
        provider_reason: str | None = None

        async for chunk in stream_obj:
            feedback = getattr(chunk, "prompt_feedback", None)
            block = getattr(feedback, "block_reason", None) if feedback is not None else None
            block_name = (getattr(block, "name", None) or str(block)) if block else None
            if block_name and block_name != "BLOCKED_REASON_UNSPECIFIED":
                provider_reason = f"prompt:{block_name}"
            cands = getattr(chunk, "candidates", None) or []
            cand = cands[0] if cands else None
            if cand is not None:
                fr = getattr(cand, "finish_reason", None)
                if fr:
                    finish_str = str(fr)
                    provider_reason = getattr(fr, "name", None) or str(fr)
            cand_content = getattr(cand, "content", None) if cand is not None else None
            parts = getattr(cand_content, "parts", None) or []

            for p in parts:
                text = getattr(p, "text", None)
                fc = getattr(p, "function_call", None)
                if text:
                    if open_text_bi is None:
                        open_text_bi = next_block_index
                        next_block_index += 1
                        yield BlockStart(block_index=open_text_bi, kind="text")
                    yield BlockDelta(block_index=open_text_bi, kind="text", text=text)
                elif fc is not None:
                    if open_text_bi is not None:
                        yield BlockStop(block_index=open_text_bi)
                        open_text_bi = None
                    bi = next_block_index
                    next_block_index += 1
                    had_tool = True
                    args_dict = dict(getattr(fc, "args", {}) or {})
                    yield BlockStart(
                        block_index=bi,
                        kind="tool_use",
                        tool_id=f"gemini_call_{uuid.uuid4().hex}",
                        tool_name=getattr(fc, "name", "") or "",
                    )
                    if args_dict:
                        yield BlockDelta(
                            block_index=bi,
                            kind="tool_args",
                            text=json.dumps(args_dict, default=str),
                        )
                    yield BlockStop(block_index=bi)

            u = getattr(chunk, "usage_metadata", None)
            if u is not None:
                usage.input_tokens = getattr(u, "prompt_token_count", 0) or 0
                usage.output_tokens = getattr(u, "candidates_token_count", 0) or 0
                usage.cache_read_input_tokens = (
                    getattr(u, "cached_content_token_count", 0) or 0
                )

        if open_text_bi is not None:
            yield BlockStop(block_index=open_text_bi)

        if had_tool:
            stop_reason = "tool_use"
        elif "MAX_TOKENS" in finish_str:
            stop_reason = "max_tokens"
        else:
            stop_reason = "end_turn"
        yield MessageStop(stop_reason=stop_reason, usage=usage,  # type: ignore[arg-type]
                          provider_reason=provider_reason)


# ---- request translation -----------------------------------------------


def _messages_to_contents(messages: list[Message]) -> list[dict[str, Any]]:
    """Anthropic blocks → Gemini ``contents`` list."""

    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "user":
            parts: list[dict[str, Any]] = []
            for b in m.content:
                if b.get("type") == "text" and b.get("text"):
                    parts.append({"text": b["text"]})
                elif b.get("type") == "image":
                    src = b.get("source") or {}
                    parts.append(
                        {
                            "inline_data": {
                                "mime_type": src.get("media_type", "image/png"),
                                "data": src.get("data", ""),
                            }
                        }
                    )
                elif b.get("type") == "tool_result":
                    raw = b.get("content", "")
                    response: Any
                    if isinstance(raw, str):
                        try:
                            parsed = json.loads(raw)
                            response = (
                                parsed
                                if isinstance(parsed, dict) and not isinstance(parsed, list)
                                else {"result": parsed}
                            )
                        except json.JSONDecodeError:
                            response = {"result": raw}
                    else:
                        response = raw if isinstance(raw, dict) else {"result": raw}
                    if b.get("is_error"):
                        response = {**response, "error": True}
                    parts.append(
                        {
                            "function_response": {
                                "name": b.get("_name", "unknown_tool"),
                                "response": response,
                            }
                        }
                    )
            if parts:
                out.append({"role": "user", "parts": parts})
        else:  # assistant
            parts = []
            for b in m.content:
                if b.get("type") == "text" and b.get("text"):
                    parts.append({"text": b["text"]})
                elif b.get("type") == "tool_use":
                    parts.append(
                        {
                            "function_call": {
                                "name": b["name"],
                                "args": b.get("input") or {},
                            }
                        }
                    )
            if parts:
                out.append({"role": "model", "parts": parts})
    return out


def _sanitize_schema(input_schema: Any) -> Any:
    """Drop JSON-Schema keywords Gemini doesn't accept; mirror the
    plugin-light sanitiser so behaviour is identical."""

    if isinstance(input_schema, list):
        return input_schema
    if not isinstance(input_schema, dict):
        return input_schema

    out: dict[str, Any] = {}
    for k, v in input_schema.items():
        if k in ("type", "description", "nullable", "format"):
            out[k] = v
        elif k in ("enum", "required") and isinstance(v, list):
            out[k] = list(v)
        elif k == "items":
            out[k] = _sanitize_schema(v)
        elif k == "properties" and isinstance(v, dict):
            out[k] = {pn: _sanitize_schema(ps) for pn, ps in v.items()}

    if out.get("type") == "object" and not out.get("properties"):
        out.pop("properties", None)
    if out.get("type") == "array" and "items" not in out:
        out["items"] = {"type": "object"}
    if isinstance(out.get("required"), list):
        prop_keys = set((out.get("properties") or {}).keys())
        filtered = [k for k in out["required"] if isinstance(k, str) and k in prop_keys]
        if filtered:
            out["required"] = filtered
        else:
            out.pop("required", None)
    return out


