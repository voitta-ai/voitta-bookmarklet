"""The fence between hg.voitta.ai and everything else on this server.

Added as the OUTERMOST layer of the main app. A request whose Host is an hg host is handed to the
hg app (app.hg.api + /mcp) and never reaches the main app's middleware or routes — not the chat
(/chainlit), not /api (settings, workspace, Python, admin, plugins), not the main /mcp, not the
widget. Unknown paths on the hg host are a plain 404 from the hg app.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Receive, Scope, Send

from app import hg


class HgHostFence:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._hg: ASGIApp | None = None

    def _hg_app(self) -> ASGIApp:
        if self._hg is None:
            from app.hg import api, mcp

            api.app.mount("/mcp", mcp.asgi())
            self._hg = api.app
        return self._hg

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket") and hg.is_hg_request(scope):
            await self._hg_app()(scope, receive, send)
            return
        await self.app(scope, receive, send)
