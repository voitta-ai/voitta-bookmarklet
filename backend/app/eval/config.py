"""Eval API switch, tenants and storage location."""

from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path

from app.config import USER_DATA_ROOT

SCHEMA_VERSION = "1.0-proposal"


def _tenants() -> dict[str, str]:
    """``VOITTA_EVAL_TOKENS="tenant-a=token-a,tenant-b=token-b"`` -> {token_sha: tenant}.

    Each token is one tenant. Tokens are compared by SHA-256 so the plaintext
    is not kept around after parsing.
    """
    out: dict[str, str] = {}
    for pair in (os.environ.get("VOITTA_EVAL_TOKENS") or "").split(","):
        tenant, sep, token = pair.strip().partition("=")
        if sep and tenant and token:
            out[hashlib.sha256(token.encode()).hexdigest()] = tenant
    return out


def enabled() -> bool:
    return bool(_tenants())


def tenant_for_token(token: str) -> str | None:
    digest = hashlib.sha256(token.encode()).hexdigest()
    for known, tenant in _tenants().items():
        if hmac.compare_digest(known, digest):
            return tenant
    return None


def tenant_dir(tenant: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in tenant)
    path = USER_DATA_ROOT / "eval" / safe
    (path / "runs").mkdir(parents=True, exist_ok=True)
    return path


def all_tenant_dirs() -> list[Path]:
    root = USER_DATA_ROOT / "eval"
    return [p for p in root.iterdir() if p.is_dir()] if root.is_dir() else []
