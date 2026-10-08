"""MCP dump file writer.

Large MCP payloads (page HTML, devtools logs) are written here instead of
being returned inline. The MCP tool returns the file path; the caller reads
it with Claude Code's Read tool.

Dump directory:  ~/Library/Application Support/Voitta Bookmarklet/mcp_dumps/
Lifetime:        cleared on every app launch (see __main__.py).
Naming:          {session8}_{label}_{unix_ms}{suffix}
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


_DUMPS_DIR_ENV = "VOITTA_PROJECT_ROOT"  # set by __main__.py to the user data dir


def _dumps_dir() -> Path:
    root = os.environ.get(_DUMPS_DIR_ENV)
    if root:
        d = Path(root) / "mcp_dumps"
    else:
        # Dev / test fallback — use a sibling of the project root.
        d = Path.home() / "Library" / "Application Support" / "Voitta Bookmarklet" / "mcp_dumps"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_text(session_id: str, label: str, content: str, suffix: str = ".txt") -> Path:
    """Write ``content`` to a new dump file and return its path."""
    ts = int(time.time() * 1000)
    fname = f"{session_id[:8]}_{label}_{ts}{suffix}"
    path = _dumps_dir() / fname
    path.write_text(content, encoding="utf-8")
    return path


def write_json(session_id: str, label: str, data: Any) -> Path:
    """Serialise ``data`` as pretty JSON and write to a dump file."""
    return write_text(session_id, label, json.dumps(data, indent=2, default=str), ".json")
