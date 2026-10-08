"""Structural redaction for everything written to an eval trace."""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

_SECRET_KEY = re.compile(r"(api[_-]?key|token|secret|password|authorization|cookie)", re.I)
_SECRET_VALUE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{16,}|Bearer\s+[A-Za-z0-9._\-]{16,}|xox[abpsc]-[A-Za-z0-9\-]{10,})"
)


class Redactor:
    """Replaces known secret values anywhere, values under secret-looking keys,
    and strings shaped like common credentials. Returns the redacted copy and
    the JSON paths that were changed."""

    def __init__(self, secrets: list[str]) -> None:
        self._secrets = [s for s in secrets if s and len(s) >= 8]

    def scrub(self, obj: Any, path: str = "$") -> tuple[Any, list[str]]:
        hits: list[str] = []
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                sub = f"{path}.{k}"
                # Whole subtree under a secret-looking key; numbers and
                # booleans are not secrets (usage has input_tokens etc.).
                if _SECRET_KEY.search(str(k)) and v is not None and not isinstance(v, (bool, int, float)):
                    out[k] = REDACTED
                    hits.append(sub)
                else:
                    out[k], h = self.scrub(v, sub)
                    hits += h
            return out, hits
        if isinstance(obj, list):
            out_list = []
            for i, v in enumerate(obj):
                val, h = self.scrub(v, f"{path}[{i}]")
                out_list.append(val)
                hits += h
            return out_list, hits
        if isinstance(obj, str):
            new = obj
            for s in self._secrets:
                new = new.replace(s, REDACTED)
            new = _SECRET_VALUE.sub(REDACTED, new)
            if new != obj:
                hits.append(path)
            return new, hits
        return obj, hits
