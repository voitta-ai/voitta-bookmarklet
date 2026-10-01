"""Codex (ChatGPT subscription) adapter.

Drives the same Responses API as :mod:`app.services.llm.openai`, but
against the ChatGPT backend the ``codex`` CLI uses, authenticated with
the CLI's ChatGPT-plan OAuth token instead of a platform API key.

Credentials come from ``$CODEX_HOME/auth.json`` (default
``~/.codex/auth.json``), which the ``codex`` CLI owns. The file is
re-read on every request so a token the CLI rotated is picked up
without a restart. We never refresh or write the file — that would race
the CLI; ``codex login`` (or any ``codex`` run) keeps it fresh. We only
warn when the token is close to expiry.

Endpoint contract (mirrors the CLI):

  POST https://chatgpt.com/backend-api/codex/responses
  headers: Authorization: Bearer <access_token>,
           ChatGPT-Account-Id: <account_id>,
           originator / User-Agent: codex_cli_rs, session_id: <uuid4>
  body:    stream=true and store=false are mandatory; no
           max_output_tokens; tools are flat with strict=false.

The model list is read from the CLI's own ``models_cache.json`` (the
models the plan offers), so listing makes no network call.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

from app.services.llm.base import NormalisedRequest
from app.services.llm.openai import OpenAIProvider, _OpenAIStreamCM

logger = logging.getLogger(__name__)

CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
CODEX_ORIGINATOR = "codex_cli_rs"
CODEX_TIMEOUT_S = 300.0
# Warn this long before the access token expires, at most once an hour.
_EXPIRY_WARN_S = 24 * 60 * 60
_WARN_INTERVAL_S = 60 * 60
_last_warn_at = 0.0


def codex_home() -> Path:
    raw = os.environ.get("CODEX_HOME")
    retval = Path(raw).expanduser() if raw else Path.home() / ".codex"
    return retval


def load_auth() -> tuple[str, str | None] | None:
    """Return ``(access_token, account_id)`` from the CLI's auth.json, or None."""
    retval: tuple[str, str | None] | None = None
    try:
        data = json.loads((codex_home() / "auth.json").read_text(encoding="utf-8"))
        tokens = data.get("tokens") or {}
        token = tokens.get("access_token")
        if isinstance(token, str) and token:
            account_id = tokens.get("account_id")
            retval = (token, account_id if isinstance(account_id, str) and account_id else None)
    except FileNotFoundError:
        pass
    except Exception:
        logger.warning("codex: could not read %s", codex_home() / "auth.json", exc_info=True)
    return retval


def access_token() -> str | None:
    auth = load_auth()
    retval = auth[0] if auth else None
    return retval


def _token_expiry(token: str) -> float | None:
    """Unverified read of the JWT ``exp`` claim — only used for a warning."""
    retval: float | None = None
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        if isinstance(exp, (int, float)):
            retval = float(exp)
    except Exception:
        pass
    return retval


def _warn_if_expiring(token: str) -> None:
    global _last_warn_at
    exp = _token_expiry(token)
    now = time.time()
    if exp is None or exp - now > _EXPIRY_WARN_S or now - _last_warn_at < _WARN_INTERVAL_S:
        return
    _last_warn_at = now
    logger.warning(
        "codex: ChatGPT access token %s; run `codex login` (or any codex command) to refresh",
        "has expired" if exp <= now else f"expires in {int((exp - now) / 3600)}h",
    )


class CodexProvider(OpenAIProvider):
    id = "codex"

    def __init__(self, api_key: str | None = None) -> None:
        # The credential is re-read from auth.json per request; the
        # ``api_key`` argument exists only to match the factory signature.
        pass

    async def list_models(self) -> list[str]:
        """Models the ChatGPT plan offers, from the CLI's models_cache.json."""
        retval: list[str] = []
        try:
            data = json.loads((codex_home() / "models_cache.json").read_text(encoding="utf-8"))
            listed = [
                m for m in (data.get("models") or [])
                if isinstance(m, dict)
                and isinstance(m.get("slug"), str)
                and m.get("visibility") == "list"
            ]
            listed.sort(key=lambda m: m.get("priority") or 0)
            retval = [m["slug"] for m in listed]
        except FileNotFoundError:
            pass
        return retval

    def _build_kwargs(self, req: NormalisedRequest) -> dict[str, Any]:
        kwargs = super()._build_kwargs(req)
        # The ChatGPT backend rejects max_output_tokens and requires store=false.
        kwargs.pop("max_output_tokens", None)
        kwargs["store"] = False
        for tool in kwargs.get("tools") or []:
            tool["strict"] = False
        return kwargs

    def stream(self, req: NormalisedRequest):
        auth = load_auth()
        if auth is None:
            raise RuntimeError(
                f"codex: not signed in — no access token in {codex_home() / 'auth.json'}; run `codex login`"
            )
        token, account_id = auth
        _warn_if_expiring(token)
        headers = {
            "originator": CODEX_ORIGINATOR,
            "User-Agent": CODEX_ORIGINATOR,
            "session_id": str(uuid.uuid4()),
        }
        if account_id:
            headers["ChatGPT-Account-Id"] = account_id
        client = AsyncOpenAI(
            api_key=token,
            base_url=CODEX_BASE_URL,
            default_headers=headers,
            timeout=CODEX_TIMEOUT_S,
        )
        retval = _OpenAIStreamCM(client, self._build_kwargs(req))
        return retval
