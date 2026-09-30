"""Tool-use agent loop, Chainlit-flavoured.

Direct port of the old ``routes/chat.py:_stream()`` agent loop, with
two differences:

* No SSE framing. Streaming text goes through ``cl.Message.stream_token``;
  every tool call gets its own ``cl.Step(type="tool")``; the per-iteration
  ``turn_persist`` mechanism is gone — history lives in
  ``cl.user_session["messages"]`` and the caller mutates it in place.
* The "browser-side" tools we used to dispatch via the bridge bus now
  go through ``cl.CopilotFunction.acall()`` (see
  :mod:`app.tools.registry`), which round-trips through the React
  client's ``call_fn`` socket event.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any

import chainlit as cl

from app.config import DEFAULT_MAX_TOKENS, DEFAULT_MAX_TOOL_ITERATIONS
from app.settings import load as load_user_settings
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
from app.tools.registry import ToolCtx, registry

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


async def run_turn(
    *,
    messages: list[LlmMessage],
    system: str,
    provider_id: ProviderId,
    api_key: str,
    model: str | None,
    ctx: ToolCtx,
) -> None:
    """Drive the agent loop until ``stop_reason != "tool_use"``.

    Mutates ``messages`` in place — appends one assistant message per
    iteration, plus a synthetic tool_result user message between
    iterations. Emits Chainlit primitives along the way.
    """
    provider = get_provider(provider_id, api_key)
    use_model = model or default_model_for(provider_id)
    visible = registry.visible_for_host(ctx.host)
    all_names = [s.name for s in registry.all()]
    visible_names = [s.name for s in visible]
    hidden_names = sorted(set(all_names) - set(visible_names))
    logger.info(
        "run_turn: host=%r visible=%d/%d hidden=%s",
        ctx.host, len(visible_names), len(all_names), hidden_names,
    )
    tools = [
        ToolSchema(name=s.name, description=s.description, input_schema=s.input_schema)
        for s in visible
    ]
    # Read the cap from settings at the top of the turn so changes in
    # the Global tab take effect on the next user message without a
    # backend restart.
    _user_settings = load_user_settings()
    max_iters = _user_settings.get("max_tool_iterations", DEFAULT_MAX_TOOL_ITERATIONS)
    max_tokens = _user_settings.get("max_tokens", DEFAULT_MAX_TOKENS)

    for iteration in range(max_iters):
        streaming_msg: cl.Message | None = None
        # Per-iteration accumulated blocks, indexed by provider block_index.
        blocks_by_index: dict[int, dict[str, Any]] = {}
        text_buf: dict[int, list[str]] = {}
        args_buf: dict[int, list[str]] = {}
        steps_by_index: dict[int, cl.Step] = {}
        iter_stop_reason = "end_turn"

        async with provider.stream(
            NormalisedRequest(
                model=use_model,
                system=system,
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
                        step = cl.Step(name=ev.tool_name or "tool", type="tool")
                        step.input = ""
                        await step.send()
                        steps_by_index[ev.block_index] = step
                elif isinstance(ev, BlockDelta):
                    if ev.kind == "text":
                        text_buf.setdefault(ev.block_index, []).append(ev.text)
                        if ev.text:
                            if streaming_msg is None:
                                streaming_msg = cl.Message(content="")
                                await streaming_msg.send()
                            await streaming_msg.stream_token(ev.text)
                    else:
                        args_buf.setdefault(ev.block_index, []).append(ev.text)
                        step = steps_by_index.get(ev.block_index)
                        if step is not None and ev.text:
                            step.input = (step.input or "") + ev.text
                            await step.update()
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
                elif isinstance(ev, StreamError):
                    if streaming_msg is not None:
                        await streaming_msg.update()
                    raise RuntimeError(f"{ev.type}: {ev.message}")

        if streaming_msg is not None:
            await streaming_msg.update()

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
                await cl.Message(
                    content=(
                        f"⚠️ Response truncated at the **max_tokens={max_tokens}** "
                        "cap. Raise it in ⚙ Settings → Global → "
                        "*Max response tokens per turn*, or ask me to continue."
                    )
                ).send()
            return

        # tool_use path: dispatch in parallel, attach results to steps,
        # then loop.
        tool_uses = [
            (idx, blocks_by_index[idx])
            for idx in sorted(blocks_by_index)
            if blocks_by_index[idx]["type"] == "tool_use"
        ]
        results = await asyncio.gather(
            *[
                registry.dispatch(tu["name"], dict(tu.get("input") or {}), ctx)
                for _, tu in tool_uses
            ]
        )

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
                                await cl.Message(
                                    content=f"📸 `{label}`",
                                    elements=[cl.Image(
                                        name=f"{label}.{ext}",
                                        content=full_bytes,
                                        mime=mime_full,
                                        display="inline",
                                    )],
                                ).send()
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
                step.output = _tool_result_text(content_payload)
                if not res.ok:
                    step.is_error = True
                # Attach every captured image (both LLM-visible and
                # chat-only) as an inline element on the tool step so
                # the human sees every candidate. Labels carry the
                # technique/strategy so filenames are self-explanatory
                # in the Chainlit chips.
                elements = []
                base = tu.get("name") or "screenshot"
                for idx, (blk, label) in enumerate(zip(image_blocks, image_labels)):
                    # Stash-path images already posted as standalone
                    # chat messages — don't duplicate.
                    if idx < len(image_already_in_chat) and image_already_in_chat[idx]:
                        continue
                    try:
                        raw = base64.b64decode(blk["source"]["data"])
                        elements.append(
                            cl.Image(
                                name=f"{base}__{label}.png",
                                content=raw,
                                mime=blk["source"]["media_type"],
                                display="inline",
                            )
                        )
                    except Exception:
                        logger.exception(
                            "failed to attach screenshot element (%s)", label,
                        )
                for img, label in zip(chat_only_images, chat_only_labels):
                    try:
                        raw = base64.b64decode(img["data"])
                        elements.append(
                            cl.Image(
                                name=f"{base}__{label}.png",
                                content=raw,
                                mime=img["media_type"],
                                display="inline",
                            )
                        )
                    except Exception:
                        logger.exception(
                            "failed to attach chat-only screenshot (%s)", label,
                        )
                if elements:
                    step.elements = elements
                await step.update()

            # ponytail: Requesty silently drops tool_result images for
            # non-Claude models (model goes blind, no error); gate on the
            # model id if that combination matters.
            if image_blocks and provider_id in ("anthropic", "requesty"):
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
    await cl.Message(content=f"⚠️ tool-use loop exceeded {max_iters} iterations").send()
