"""Tool-use agent loop for the API providers.

Direct port of the old ``routes/chat.py:_stream()`` agent loop, with
two differences:

* No SSE framing, and no UI primitives in the loop. Everything the loop
  shows (streamed text, one step per tool call, notices, full-size
  screenshots) goes through a :class:`TurnSink`. :class:`ChainlitSink`
  renders it as ``cl.Message``/``cl.Step`` exactly as before; other
  transports (an eval API, voitta-bookmarklet#14) supply their own sink.
  History is the ``messages`` list, which the caller owns and the loop
  mutates in place.
* The "browser-side" tools we used to dispatch via the bridge bus now
  go through ``cl.CopilotFunction.acall()`` (see
  :mod:`app.tools.registry`), which round-trips through the React
  client's ``call_fn`` socket event.

Everything a turn depends on arrives in a :class:`RunContext`, including
a snapshot of the settings it uses, so a settings change mid-turn cannot
alter a running turn.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

import chainlit as cl

from app.services.llm import (
    NormalisedRequest,
    ProviderId,
    ToolSchema,
    default_model_for,
    get_provider,
)
from app.services.llm.base import Message as LlmMessage
from app.services.llm.stream import (
    BlockDelta,
    BlockStart,
    BlockStop,
    MessageStop,
    StreamError,
)
from app.tools.registry import ToolCtx, ToolResult, registry

logger = logging.getLogger(__name__)


MAX_TOOL_RESULT_BYTES = 32_000


def _tool_result_text(result: Any) -> str:
    text = (
        result if isinstance(result, str)
        else json.dumps(result, ensure_ascii=False, default=str)
    )
    if len(text) <= MAX_TOOL_RESULT_BYTES:
        return text
    return text[:MAX_TOOL_RESULT_BYTES] + f"\n…[truncated: {len(text)} bytes]"


@dataclass(frozen=True)
class Attachment:
    """An image shown to the human alongside a tool result."""

    name: str
    content: bytes
    mime: str


class TurnSink(Protocol):
    """Where a turn's observable output goes.

    Text arrives as deltas; :meth:`text_end` closes the current text
    bubble (the next delta starts a new one). Each tool call is opened
    with :meth:`tool_start`, which returns an opaque handle passed back to
    :meth:`tool_input_delta` and :meth:`tool_end`.
    """

    async def text_delta(self, text: str) -> None: ...

    async def text_end(self) -> None: ...

    async def tool_start(self, name: str) -> Any: ...

    async def tool_input_delta(self, handle: Any, text: str) -> None: ...

    async def tool_end(
        self, handle: Any, output: str, is_error: bool, attachments: list[Attachment],
    ) -> None: ...

    async def image(self, label: str, attachment: Attachment) -> None:
        """A full-size image posted to the conversation itself."""
        ...

    async def notice(self, text: str) -> None:
        """A message from the loop (truncation, iteration cap)."""
        ...

    async def model_request(self, model: str, message_count: int) -> None:
        """A provider call is about to be made."""
        ...

    async def model_stop(self, stop_reason: str, usage: Any) -> None:
        """The provider call finished with this stop reason and usage."""
        ...


class ChainlitSink:
    """Renders a turn as Chainlit messages and steps (the chat UI)."""

    def __init__(self) -> None:
        self._msg: cl.Message | None = None

    async def text_delta(self, text: str) -> None:
        if not text:
            return
        msg = self._msg
        if msg is None:
            msg = self._msg = cl.Message(content="")
            await msg.send()
        await msg.stream_token(text)

    async def text_end(self) -> None:
        if self._msg is not None:
            await self._msg.update()
            self._msg = None

    async def tool_start(self, name: str) -> cl.Step:
        step = cl.Step(name=name, type="tool")
        step.input = ""
        await step.send()
        return step

    async def tool_input_delta(self, handle: cl.Step, text: str) -> None:
        if text:
            handle.input = (handle.input or "") + text
            await handle.update()

    async def tool_end(
        self, handle: cl.Step, output: str, is_error: bool, attachments: list[Attachment],
    ) -> None:
        handle.output = output
        if is_error:
            handle.is_error = True
        if attachments:
            handle.elements = [
                cl.Image(name=a.name, content=a.content, mime=a.mime, display="inline")
                for a in attachments
            ]
        await handle.update()

    async def image(self, label: str, attachment: Attachment) -> None:
        await cl.Message(
            content=f"📸 `{label}`",
            elements=[cl.Image(
                name=attachment.name,
                content=attachment.content,
                mime=attachment.mime,
                display="inline",
            )],
        ).send()

    async def notice(self, text: str) -> None:
        await cl.Message(content=text).send()

    async def model_request(self, model: str, message_count: int) -> None:
        pass

    async def model_stop(self, stop_reason: str, usage: Any) -> None:
        pass


# (name, args, ctx, tool_use_id) -> result. The default is registry.dispatch.
Dispatch = Callable[[str, dict[str, Any], ToolCtx, str], Awaitable[ToolResult]]


@dataclass(frozen=True)
class RunContext:
    """Everything one turn depends on, resolved by the caller up front.

    ``max_tokens`` and ``max_tool_iterations`` are a snapshot of the
    user's settings taken before the turn starts.
    """

    provider_id: ProviderId
    api_key: str
    model: str | None
    system: str
    tool_ctx: ToolCtx
    max_tokens: int
    max_tool_iterations: int
    sink: TurnSink
    # Overrides for non-UI transports (the eval API): the tool list shown to
    # the model, and the function that executes a tool call. None means the
    # registry's host-visible tools and registry.dispatch.
    tools: tuple[ToolSchema, ...] | None = None
    dispatch: Dispatch | None = None


async def run_turn(*, messages: list[LlmMessage], run: RunContext) -> None:
    """Drive the agent loop until ``stop_reason != "tool_use"``.

    Mutates ``messages`` in place — appends one assistant message per
    iteration, plus a synthetic tool_result user message between
    iterations. Emits its output through ``run.sink``.
    """
    provider_id = run.provider_id
    ctx = run.tool_ctx
    sink = run.sink
    provider = get_provider(provider_id, run.api_key)
    use_model = run.model or default_model_for(provider_id)
    visible = registry.visible_for_host(ctx.host)
    all_names = [s.name for s in registry.all()]
    visible_names = [s.name for s in visible]
    hidden_names = sorted(set(all_names) - set(visible_names))
    logger.info(
        "run_turn: host=%r visible=%d/%d hidden=%s",
        ctx.host, len(visible_names), len(all_names), hidden_names,
    )
    tools = list(run.tools) if run.tools is not None else [
        ToolSchema(name=s.name, description=s.description, input_schema=s.input_schema)
        for s in visible
    ]
    max_iters = run.max_tool_iterations
    max_tokens = run.max_tokens

    for iteration in range(max_iters):
        # Per-iteration accumulated blocks, indexed by provider block_index.
        blocks_by_index: dict[int, dict[str, Any]] = {}
        text_buf: dict[int, list[str]] = {}
        args_buf: dict[int, list[str]] = {}
        steps_by_index: dict[int, Any] = {}
        iter_stop_reason = "end_turn"

        await sink.model_request(use_model, len(messages))
        async with provider.stream(
            NormalisedRequest(
                model=use_model,
                system=run.system,
                max_tokens=max_tokens,
                messages=messages,
                tools=tools,
            )
        ) as events:
            async for ev in events:
                if isinstance(ev, BlockStart):
                    if ev.kind == "text":
                        blocks_by_index[ev.block_index] = {"type": "text", "text": ""}
                        text_buf[ev.block_index] = []
                    else:
                        blocks_by_index[ev.block_index] = {
                            "type": "tool_use",
                            "id": ev.tool_id or "",
                            "name": ev.tool_name or "",
                            "input": {},
                        }
                        args_buf[ev.block_index] = []
                        steps_by_index[ev.block_index] = await sink.tool_start(
                            ev.tool_name or "tool",
                        )
                elif isinstance(ev, BlockDelta):
                    if ev.kind == "text":
                        text_buf.setdefault(ev.block_index, []).append(ev.text)
                        await sink.text_delta(ev.text)
                    else:
                        args_buf.setdefault(ev.block_index, []).append(ev.text)
                        step = steps_by_index.get(ev.block_index)
                        if step is not None:
                            await sink.tool_input_delta(step, ev.text)
                elif isinstance(ev, BlockStop):
                    block = blocks_by_index.get(ev.block_index)
                    if block is None:
                        continue
                    if block["type"] == "text":
                        block["text"] = "".join(text_buf.get(ev.block_index, []))
                    else:
                        joined = "".join(args_buf.get(ev.block_index, ""))
                        try:
                            block["input"] = json.loads(joined) if joined else {}
                        except json.JSONDecodeError:
                            block["input"] = {"_raw": joined}
                elif isinstance(ev, MessageStop):
                    iter_stop_reason = ev.stop_reason
                    await sink.model_stop(ev.stop_reason, ev.usage)
                elif isinstance(ev, StreamError):
                    await sink.text_end()
                    raise RuntimeError(f"{ev.type}: {ev.message}")

        await sink.text_end()

        # Assemble the assistant turn in block_index order.
        assistant_content = [
            blocks_by_index[i]
            for i in sorted(blocks_by_index)
            if not (blocks_by_index[i]["type"] == "text" and not blocks_by_index[i].get("text"))
        ]

        if iter_stop_reason != "tool_use":
            # Drop orphan tool_use blocks (model hit max_tokens mid-call).
            persistable = [b for b in assistant_content if b.get("type") != "tool_use"]
            if persistable:
                messages.append(LlmMessage(role="assistant", content=persistable))
            if iter_stop_reason == "max_tokens":
                # Surface the truncation so the user sees *why* the
                # response stopped — otherwise the partial bubble looks
                # like a hang. The number echoed is the effective cap
                # for this turn, set by the Global settings tab.
                await sink.notice(
                    f"⚠️ Response truncated at the **max_tokens={max_tokens}** "
                    "cap. Raise it in ⚙ Settings → Global → "
                    "*Max response tokens per turn*, or ask me to continue."
                )
            return

        # tool_use path: dispatch in parallel, attach results to steps,
        # then loop.
        tool_uses = [
            (idx, blocks_by_index[idx])
            for idx in sorted(blocks_by_index)
            if blocks_by_index[idx]["type"] == "tool_use"
        ]
        if run.dispatch is not None:
            dispatch = run.dispatch
            calls = [
                dispatch(tu["name"], dict(tu.get("input") or {}), ctx, tu["id"])
                for _, tu in tool_uses
            ]
        else:
            calls = [
                registry.dispatch(tu["name"], dict(tu.get("input") or {}), ctx)
                for _, tu in tool_uses
            ]
        results = await asyncio.gather(*calls)

        tool_result_blocks: list[dict[str, Any]] = []
        for (block_idx, tu), res in zip(tool_uses, results):
            step = steps_by_index.get(block_idx)
            content_payload = res.result if res.ok else {"error": res.error or {"kind": "error", "message": "tool failed"}}

            # Image sentinels: a browser tool can return:
            #   {"_image":  {media_type, data}}                   — one (legacy)
            #   {"_images": [{label, media_type, data}, ...]}     — many, model sees them
            #   {"_images_chat_only": [...]}                      — many, chat only
            #
            # ``_images_chat_only`` is the evaluation mode for
            # screenshot_report — produces many strategy×technique
            # candidates that should appear as inline-chat thumbnails
            # for the human to compare, WITHOUT inlining the pixels
            # into the LLM context (too much, and the model isn't
            # picking strategies during eval). The LLM only sees the
            # structured metadata (label, strategy, technique,
            # target_height, ms) so it can describe what was tried.
            # ``image_blocks`` → inlined into the Anthropic tool_result
            # so the LLM can SEE the image. These come from the
            # ``_image`` (legacy single) and ``_images`` (legacy multi)
            # sentinels, AND from the downsized webp generated when
            # consuming ``_images_stash``. Stay small (<200 KB each).
            #
            # ``chat_only_images`` → attached to the tool step's
            # collapsed area only. The ``_images_chat_only`` legacy
            # sentinel uses this. The new ``_images_stash`` path
            # posts FULL-SIZE originals as separate cl.Message calls
            # below, not via this list, so they show up in the main
            # chat stream rather than being hidden in the tool step.
            image_blocks: list[dict[str, Any]] = []
            image_labels: list[str] = []
            # Per image_block: True if the FULL-SIZE original has already
            # been posted as its own cl.Message (stash path). In that
            # case, do NOT also attach the downsized version to the
            # tool-step elements — it'd duplicate.
            image_already_in_chat: list[bool] = []
            chat_only_images: list[dict[str, Any]] = []
            chat_only_labels: list[str] = []
            if isinstance(content_payload, dict):
                if "_image" in content_payload:
                    content_payload = dict(content_payload)
                    img = content_payload.pop("_image", None)
                    if (
                        isinstance(img, dict)
                        and isinstance(img.get("data"), str)
                        and isinstance(img.get("media_type"), str)
                    ):
                        image_blocks.append({
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": img["media_type"],
                                "data": img["data"],
                            },
                        })
                        image_labels.append("screenshot")
                        image_already_in_chat.append(False)
                if "_images" in content_payload:
                    content_payload = dict(content_payload)
                    imgs = content_payload.pop("_images", None)
                    if isinstance(imgs, list):
                        for i, img in enumerate(imgs):
                            if (
                                isinstance(img, dict)
                                and isinstance(img.get("data"), str)
                                and isinstance(img.get("media_type"), str)
                            ):
                                image_blocks.append({
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": img["media_type"],
                                        "data": img["data"],
                                    },
                                })
                                lbl = img.get("label")
                                image_labels.append(
                                    str(lbl) if lbl else f"shot_{i}",
                                )
                                image_already_in_chat.append(False)
                if "_images_chat_only" in content_payload:
                    # Legacy inline path (kept for shadow-DOM reports
                    # that haven't migrated to the stash). Same flow
                    # as _images_stash but bytes come inline.
                    content_payload = dict(content_payload)
                    co_imgs = content_payload.pop("_images_chat_only", None)
                    if isinstance(co_imgs, list):
                        summary = []
                        for i, img in enumerate(co_imgs):
                            if not isinstance(img, dict):
                                continue
                            data_b64 = img.get("data")
                            mt = img.get("media_type")
                            if not (isinstance(data_b64, str) and isinstance(mt, str)):
                                continue
                            label = img.get("label") or f"shot_{i}"
                            chat_only_images.append({
                                "media_type": mt,
                                "data": data_b64,
                            })
                            chat_only_labels.append(str(label))
                            meta = {k: v for k, v in img.items()
                                    if k not in ("data", "media_type")}
                            summary.append(meta)
                        content_payload["captured_chat_only"] = summary
                if "_images_stash" in content_payload:
                    # Stash path:
                    #   1. Pop the full-size bytes from the BE stash.
                    #   2. Post the FULL-SIZE original as a separate
                    #      Chainlit message so the user sees it in
                    #      the chat stream (not buried in the tool
                    #      step's collapsed Tool Output).
                    #   3. Downsize to a sensible webp (max 1280x2400,
                    #      quality 75) and include THAT in the
                    #      tool_result as an inline image block so
                    #      the LLM can see the layout without burning
                    #      context on a multi-MB PNG.
                    #
                    # If multiple stash entries arrive (all_techniques
                    # eval mode), every full-size posts as a separate
                    # chat message and every downsized version goes
                    # into the LLM context.
                    from app.main import _screenshot_stash_pop
                    content_payload = dict(content_payload)
                    stash_refs = content_payload.pop("_images_stash", None)
                    if isinstance(stash_refs, list):
                        summary = []
                        for i, ref in enumerate(stash_refs):
                            if not isinstance(ref, dict):
                                continue
                            sid = ref.get("stash_id")
                            if not isinstance(sid, str):
                                continue
                            entry = _screenshot_stash_pop(sid)
                            if entry is None:
                                # Already evicted / TTL expired / never
                                # uploaded. Surface as a per-image error
                                # so the user sees what was lost.
                                summary.append({
                                    **{k: v for k, v in ref.items() if k != "stash_id"},
                                    "stash_miss": True,
                                })
                                continue
                            label = str(ref.get("label") or f"shot_{i}")
                            mime_full = entry["media_type"]
                            try:
                                full_bytes = base64.b64decode(entry["data"])
                            except Exception:
                                logger.exception("base64 decode failed for %s", label)
                                continue

                            # (a) Post full-size as a separate chat
                            # message so it lands in the conversation
                            # stream, not the collapsed tool step.
                            try:
                                ext = "png" if mime_full == "image/png" else "webp"
                                await sink.image(label, Attachment(
                                    name=f"{label}.{ext}",
                                    content=full_bytes,
                                    mime=mime_full,
                                ))
                            except Exception:
                                logger.exception(
                                    "failed to post full-size chat msg for %s", label,
                                )

                            # (b) Downsize for LLM context. Cap both
                            # dimensions so a tall report (e.g. 1920x
                            # 6000) doesn't produce a 1280x4000 webp.
                            # Image.thumbnail() preserves aspect ratio
                            # within the bounding box.
                            try:
                                from io import BytesIO
                                from PIL import Image as _PILImage
                                img = _PILImage.open(BytesIO(full_bytes))
                                if img.mode not in ("RGB", "RGBA"):
                                    img = img.convert("RGBA")
                                img.thumbnail((1280, 2400), _PILImage.LANCZOS)
                                buf = BytesIO()
                                # Flatten alpha to white before webp
                                # so transparent PNG backgrounds don't
                                # render as black.
                                if img.mode == "RGBA":
                                    bg = _PILImage.new("RGB", img.size, (255, 255, 255))
                                    bg.paste(img, mask=img.split()[-1])
                                    img = bg
                                img.save(buf, format="WEBP", quality=75, method=4)
                                webp_bytes = buf.getvalue()
                                webp_b64 = base64.b64encode(webp_bytes).decode("ascii")
                                image_blocks.append({
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": "image/webp",
                                        "data": webp_b64,
                                    },
                                })
                                image_labels.append(label)
                                # Full-size already posted as its own
                                # cl.Message above — don't dupe it in
                                # the step elements.
                                image_already_in_chat.append(True)
                                meta = {k: v for k, v in ref.items()
                                        if k != "stash_id"}
                                meta["full_bytes"] = len(full_bytes)
                                meta["context_webp_bytes"] = len(webp_bytes)
                                meta["context_dims"] = list(img.size)
                                summary.append(meta)
                            except Exception as exc:
                                logger.exception(
                                    "downsize failed for %s", label,
                                )
                                summary.append({
                                    **{k: v for k, v in ref.items()
                                       if k != "stash_id"},
                                    "downsize_error": str(exc),
                                })
                        content_payload["captured"] = summary
            # Legacy single-block alias so the rest of the function reads naturally.
            image_block = image_blocks[0] if image_blocks else None

            if step is not None:
                # Attach every captured image (both LLM-visible and
                # chat-only) to the tool step so the human sees every
                # candidate. Labels carry the technique/strategy so
                # filenames are self-explanatory in the Chainlit chips.
                attachments: list[Attachment] = []
                base = tu.get("name") or "screenshot"
                for idx, (blk, label) in enumerate(zip(image_blocks, image_labels)):
                    # Stash-path images already posted as standalone
                    # chat messages — don't duplicate.
                    if idx < len(image_already_in_chat) and image_already_in_chat[idx]:
                        continue
                    try:
                        attachments.append(Attachment(
                            name=f"{base}__{label}.png",
                            content=base64.b64decode(blk["source"]["data"]),
                            mime=blk["source"]["media_type"],
                        ))
                    except Exception:
                        logger.exception(
                            "failed to attach screenshot element (%s)", label,
                        )
                for img, label in zip(chat_only_images, chat_only_labels):
                    try:
                        attachments.append(Attachment(
                            name=f"{base}__{label}.png",
                            content=base64.b64decode(img["data"]),
                            mime=img["media_type"],
                        ))
                    except Exception:
                        logger.exception(
                            "failed to attach chat-only screenshot (%s)", label,
                        )
                await sink.tool_end(
                    step, _tool_result_text(content_payload), not res.ok, attachments,
                )

            # Requesty silently drops tool_result images for non-Claude
            # models, so those get the textual note below instead.
            if image_blocks and (
                provider_id == "anthropic"
                or (provider_id == "requesty" and "claude" in use_model)
            ):
                text_part = _tool_result_text(content_payload)
                content_parts: list[dict[str, Any]] = [
                    {"type": "text", "text": text_part},
                ]
                # Interleave a label-text block before each image so
                # the LLM knows which technique produced which capture.
                for blk, label in zip(image_blocks, image_labels):
                    content_parts.append({
                        "type": "text",
                        "text": f"--- capture: {label} ---",
                    })
                    content_parts.append(blk)
                tool_result_blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tu["id"],
                        "content": content_parts,
                        "is_error": not res.ok,
                    }
                )
            else:
                if image_blocks:
                    # Non-anthropic provider — surface a note so the
                    # model knows images were captured but aren't visible.
                    if isinstance(content_payload, dict):
                        content_payload["_image_note"] = (
                            f"{len(image_blocks)} image(s) captured but current "
                            f"provider doesn't accept inline images in tool "
                            f"results — switch to Anthropic to view them"
                        )
                tool_result_blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tu["id"],
                        "content": _tool_result_text(content_payload),
                        "is_error": not res.ok,
                    }
                )

        messages.append(LlmMessage(role="assistant", content=assistant_content))
        messages.append(LlmMessage(role="user", content=tool_result_blocks))

    # If we hit the iteration cap, surface it instead of silently dropping.
    await sink.notice(f"⚠️ tool-use loop exceeded {max_iters} iterations")
