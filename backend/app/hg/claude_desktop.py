"""The Claude Desktop extension (.mcpb) for a Hedgehog key — one click to install.

An .mcpb is a zip with ``manifest.json`` + our tiny Node stdio⇄HTTP bridge (``mcpb/server.js``).
The key is written into the manifest's ``mcp_config.env`` so installing needs no typing.

Updating to a new key: Claude Desktop identifies an extension by ``name`` and upgrades it when a
file with the SAME name and a HIGHER ``version`` is installed (the MCPB update rule). So:

* ``name`` is fixed (``hedgehog``) — never per key, or each download would add another extension;
* ``version`` is ``1.0.<download time>``, so every download (including one after Regenerate) is
  newer than whatever is installed and replaces it, key included. It also makes each file's bytes
  differ, sidestepping Claude Desktop's habit of silently ignoring a byte-identical reinstall.

Spec: github.com/modelcontextprotocol/mcpb MANIFEST.md (manifest_version 0.3).
"""

from __future__ import annotations

import io
import json
import time
import zipfile
from pathlib import Path

NAME = "hedgehog"
_SERVER_JS = Path(__file__).with_name("mcpb") / "server.js"


def manifest(key: str, mcp_url: str, version: str) -> dict:
    return {
        "manifest_version": "0.3",
        "name": NAME,
        "display_name": "Hedgehog (StockTrak)",
        "version": version,
        "description": "Your Hedgehog Camp account in Claude: positions, orders, quotes, trading and rewards.",
        "long_description": (
            "Connects Claude to your Hedgehog Camp (StockTrak) account through the Hedgehog panel.\n\n"
            "**Keep app.stocktrak.com open with the Hedgehog panel** (bookmarklet from hg.voitta.ai): "
            "every request runs in that tab. If you regenerate your key, download this extension again "
            "from the panel and install it; it replaces this one."
        ),
        "author": {"name": "Voitta", "url": "https://hg.voitta.ai"},
        "homepage": "https://hg.voitta.ai",
        "documentation": "https://hg.voitta.ai/docs",
        "server": {
            "type": "node",
            "entry_point": "server/index.js",
            "mcp_config": {
                "command": "node",
                "args": ["${__dirname}/server/index.js"],
                "env": {"HG_URL": mcp_url, "HG_KEY": key},
            },
        },
        "tools_generated": True,
        "keywords": ["stocktrak", "hedgehog", "trading", "stocks"],
        "compatibility": {"platforms": ["darwin", "win32"], "runtimes": {"node": ">=16.0.0"}},
        "privacy_policies": ["https://voitta.ai/privacy"],
    }


def build(key: str, mcp_url: str, now: float | None = None) -> tuple[bytes, str]:
    """``(.mcpb bytes, version)`` for this key."""
    version = f"1.0.{int(now if now is not None else time.time())}"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest(key, mcp_url, version), indent=2))
        z.writestr("server/index.js", _SERVER_JS.read_text(encoding="utf-8"))
    return buf.getvalue(), version
