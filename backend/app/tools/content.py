"""Shared conversion of registry results into text and image content."""
from __future__ import annotations

import json
from typing import Any


def tool_result_content(payload: Any) -> list[dict[str, Any]]:
    """Convert a registry tool result into MCP content blocks.

    Inline image sentinels (``_image`` / ``_images``) become MCP ``image``
    blocks so screenshot-style tools remain visual under this brain. The
    stash-backed form (``_images_stash``) is summarised as text — full-size
    inlining via the BE stash is a main-loop concern and not reproduced here.
    """
    if not isinstance(payload, dict):
        return [{"type": "text", "text": str(payload)}]

    images: list[dict[str, Any]] = []
    rest = dict(payload)

    single = rest.pop("_image", None)
    if isinstance(single, dict) and isinstance(single.get("data"), str):
        images.append(single)
    for key in ("_images", "_images_chat_only"):
        many = rest.pop(key, None)
        if isinstance(many, list):
            images.extend(i for i in many if isinstance(i, dict) and isinstance(i.get("data"), str))
    stash = rest.pop("_images_stash", None)
    if isinstance(stash, list) and stash:
        rest["_images_note"] = f"{len(stash)} image(s) captured (not inlined under this brain)"

    blocks: list[dict[str, Any]] = [
        {"type": "text", "text": json.dumps(rest, ensure_ascii=False, default=str)}
    ]
    for img in images:
        mime = img.get("media_type") or img.get("mimeType") or "image/png"
        blocks.append({"type": "image", "data": img["data"], "mimeType": mime})
    return blocks

