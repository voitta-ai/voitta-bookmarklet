#!/usr/bin/env python3
"""Test the Runpod MCP connection the same way the plugin connector does.

Usage:
    RUNPOD_API_KEY=rpa_xxx python3 test_runpod_mcp.py

Talks to https://mcp.getrunpod.io/ over streamable-HTTP with a bearer token —
the exact transport, URL and auth the runpod plugin's `mcp_servers` entry uses.
If this passes, the connector will work once the key is in Settings.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

URL = "https://mcp.getrunpod.io/"
MANIFEST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "manifest.json")


async def main() -> int:
    key = os.environ.get("RUNPOD_API_KEY", "").strip()
    if not key:
        print("set RUNPOD_API_KEY first", file=sys.stderr)
        return 2

    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    transport = StreamableHttpTransport(
        url=URL, headers={"Authorization": f"Bearer {key}"}
    )

    try:
        return await _run(Client(transport))
    except Exception as exc:
        if "401" in str(exc) or "Unauthorized" in str(exc):
            print("401 Unauthorized — server reachable, key rejected.\n"
                  "Mint one at console.runpod.io -> Settings -> API Keys.",
                  file=sys.stderr)
        else:
            print(f"failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


async def _run(client) -> int:
    async with client:
        tools = await client.list_tools()
        print(f"connected — {len(tools)} tools advertised\n")

        # Cross-check the plugin's read-only allowlist against what the
        # server actually advertises — a name drift here is exactly what
        # would silently drop a tool from the plugin.
        manifest = MANIFEST if os.path.exists(MANIFEST) else ""
        if manifest:
            allow = set(json.load(open(manifest))["mcp_servers"][0]["expose_tools"])
            names = {t.name for t in tools}
            print(f"allowlisted & present : {len(allow & names)}/{len(allow)}")
            if allow - names:
                print(f"allowlisted but MISSING: {sorted(allow - names)}")
            print()

        # A real read-only call: the GPU catalog. No account state, no spend.
        print("calling list-gpu-types …")
        res = await client.call_tool("list-gpu-types", {})
        text = ""
        for block in (res.content or []):
            text += getattr(block, "text", "") or ""
        try:
            data = json.loads(text)
            rows = data if isinstance(data, list) else data.get("data", data)
            print(f"  -> {len(rows)} GPU types")
            for row in (rows[:5] if isinstance(rows, list) else []):
                if isinstance(row, dict):
                    print(f"     {row.get('id') or row.get('displayName')}"
                          f"  mem={row.get('memoryInGb')}GB")
        except Exception:
            print("  ->", text[:400])
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
