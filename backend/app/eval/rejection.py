"""Tell a provider-side rejection apart from agent behaviour (#25).

A provider can block a request before or while the model answers: a safety
classifier stops the stream, a content filter cuts the response short, or the
API refuses the request outright. None of that is the agent declining, so a
probe that hits it must not be scored as an agent outcome.

Classification uses structured provider signals first: the raw stop or finish
reason a provider reports in-band, and the error code on a provider exception.
The one text fallback is ``_MESSAGE_SIGNATURES`` below, for a rejection whose
error carries no code we have seen. Anything not listed is left as an ordinary
completion or failure.
"""

from __future__ import annotations

from typing import Any

# Raw stop/finish reasons (MessageStop.provider_reason) that mean the provider
# blocked the response. Gemini prompt blocks arrive as "prompt:<reason>".
_IN_BAND: dict[str, frozenset[str]] = {
    # Streaming safety classifiers end the response with stop_reason "refusal".
    "anthropic": frozenset({"refusal"}),
    # Responses API: incomplete_details.reason.
    "openai": frozenset({"content_filter"}),
    "codex": frozenset({"content_filter"}),
    "gemini": frozenset({
        "SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION",
        "IMAGE_SAFETY", "IMAGE_PROHIBITED_CONTENT", "IMAGE_RECITATION",
    }),
}

# Error codes on provider exceptions (and on a failed Responses stream,
# reported as provider_reason "failed:<code>") that mean a policy block.
_ERROR_CODES: dict[str, frozenset[str]] = {
    "openai": frozenset({"content_policy_violation", "invalid_prompt", "content_filter"}),
    "codex": frozenset({"content_policy_violation", "invalid_prompt", "content_filter"}),
}


# Text fallback, used only where the provider gave nothing structured. The
# Codex (ChatGPT backend) cybersecurity flag has so far arrived as a bare
# openai.APIError with no code: only its message identifies it. Every provider
# exception's code and body are recorded in run.failed, so if a code shows up
# it should replace this entry.
_MESSAGE_SIGNATURES: dict[str, tuple[str, ...]] = {
    "codex": ("flagged for possible cybersecurity risk",),
}


def in_band(provider: str, reason: str | None, message: str | None) -> dict[str, Any] | None:
    """The rejection described by a raw stop reason, or None."""
    if not reason:
        return None
    if reason.startswith("prompt:") and provider == "gemini":
        matched = True
    elif reason.startswith("failed:"):
        matched = reason.removeprefix("failed:") in _ERROR_CODES.get(provider, frozenset())
    else:
        matched = reason in _IN_BAND.get(provider, frozenset())
    if not matched:
        return None
    return {"kind": "provider_rejected", "provider": provider, "signal": "stop_reason",
            "provider_reason": reason, "message": message}


def from_exception(provider: str, exc: BaseException) -> dict[str, Any] | None:
    """The rejection described by a provider exception, or None."""
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code in _ERROR_CODES.get(provider, frozenset()):
        signal = "error_code"
    elif any(sig in str(exc) for sig in _MESSAGE_SIGNATURES.get(provider, ())):
        signal = "message_signature"
    else:
        return None
    return {"kind": "provider_rejected", "provider": provider, "signal": signal,
            **provider_error(exc), "message": str(exc)}


def provider_error(exc: BaseException) -> dict[str, Any]:
    """The structured fields a provider exception carries, for the trace."""
    retval: dict[str, Any] = {"exception": type(exc).__name__}
    for attr, key in (("code", "provider_code"), ("type", "provider_type"),
                      ("status_code", "status"), ("body", "provider_body")):
        val = getattr(exc, attr, None)
        if val is not None:
            retval[key] = val
    return retval


def describe() -> dict[str, Any]:
    """What /capabilities advertises about rejection detection."""
    return {
        "outcome": "run.failed with error.kind = provider_rejected",
        "stop_reasons": {p: sorted(v) for p, v in _IN_BAND.items()},
        "error_codes": {p: sorted(v) for p, v in _ERROR_CODES.items()},
        "gemini_prompt_blocks": "prompt:<block_reason>",
        "message_signatures": {p: list(v) for p, v in _MESSAGE_SIGNATURES.items()},
    }
