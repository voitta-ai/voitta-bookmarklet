"""Unattended browser worker: the eval runner's :class:`BrowserTransport`.

One worker per eval session. It launches its own headless Chromium with a
fresh, throwaway profile (never a user profile), loads the session's fixture
page, and refuses every request to a host outside the session's grant, so a
script run through it cannot navigate or fetch elsewhere. Deadlines are
enforced here with ``asyncio.wait_for``, not by the page.

Only the primitives the eval path needs are implemented natively
(``get_page_title``, ``eval_js``); any other primitive fails with
``tool_unavailable``. Requires the optional ``playwright`` package and its
Chromium build.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlsplit

from app.tools.browser import BrowserToolError

logger = logging.getLogger(__name__)

# Same contract as the bookmarklet's eval_js (frontend/src/lib/primitives.ts):
# async IIFE via indirect eval, console captured, page-side await_ms race.
_EVAL_JS = """
async ([js, awaitMs]) => {
  const safe = (v) => { try { JSON.stringify(v); return v; }
                        catch { try { return String(v); } catch { return "[unstringifiable]"; } } };
  const logs = [];
  const wrap = (level, orig) => (...a) => {
    try { logs.push({ level, args: a.map(safe) }); } catch {}
    try { orig.apply(console, a); } catch {}
  };
  const o = [console.log, console.warn, console.error];
  console.log = wrap("log", o[0]); console.warn = wrap("warn", o[1]); console.error = wrap("error", o[2]);
  const t0 = performance.now();
  try {
    const indirectEval = eval;
    const result = await Promise.race([
      Promise.resolve().then(() => indirectEval(`(async () => {\\n${js}\\n})()`)),
      new Promise((_, rej) => setTimeout(() => rej(new Error(`eval timed out after ${awaitMs}ms`)), awaitMs)),
    ]);
    return { ok: true, result: safe(result), logs, ms: Math.round(performance.now() - t0) };
  } catch (err) {
    return { ok: false, error: "eval_threw", message: err instanceof Error ? err.message : String(err),
             stack: err instanceof Error ? err.stack : undefined, logs, ms: Math.round(performance.now() - t0) };
  } finally {
    [console.log, console.warn, console.error] = o;
  }
}
"""


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


class BrowserWorker:
    def __init__(self, url: str, granted_hosts: frozenset[str]) -> None:
        self.url = url
        self.host = _host(url)
        self._granted = granted_hosts
        self._pw: Any = None
        self._browser: Any = None
        self._page: Any = None
        self.blocked: list[str] = []

    async def start(self) -> None:
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        try:
            self._browser = await self._pw.chromium.launch(headless=True)
            # new_context() is an in-memory, throwaway profile.
            context = await self._browser.new_context()
            await context.route("**/*", self._guard)
            self._page = await context.new_page()
            await self._page.goto(self.url, wait_until="load", timeout=15_000)
        except BaseException:
            await self.close()
            raise

    async def _guard(self, route: Any) -> None:
        url = route.request.url
        scheme = urlsplit(url).scheme
        if scheme in ("data", "blob", "about") or _host(url) in self._granted:
            await route.continue_()
        else:
            self.blocked.append(url)
            if route.request.is_navigation_request():
                # A 204 answer leaves the document in place; an abort would
                # swap the fixture for Chromium's error page.
                await route.fulfill(status=204)
            else:
                await route.abort("blockedbyclient")

    async def call(self, name: str, args: dict[str, Any], timeout_ms: int) -> Any:
        if self._page is None:
            raise BrowserToolError("tool_unavailable", "browser worker is not running")
        if name == "get_page_title":
            op = self._page.title()
        elif name == "eval_js":
            js = str(args.get("js") or "")
            await_ms = int(args.get("await_ms") or 30_000)
            op = self._page.evaluate(_EVAL_JS, [js, await_ms])
        else:
            raise BrowserToolError(
                "tool_unavailable",
                f"browser primitive {name!r} is not supported by the eval worker",
            )
        try:
            res = await asyncio.wait_for(op, timeout=timeout_ms / 1000)
        except asyncio.TimeoutError as exc:
            raise BrowserToolError(
                "timeout", f"browser primitive {name!r} gave no result within {timeout_ms}ms",
            ) from exc
        except BrowserToolError:
            raise
        except Exception as exc:  # noqa: BLE001 — playwright raises various types
            raise BrowserToolError("dispatch_failed", f"browser primitive {name!r} failed: {exc}") from exc
        return {"title": res} if name == "get_page_title" else res

    async def close(self) -> None:
        for obj in (self._browser, self._pw):
            if obj is None:
                continue
            try:
                await (obj.close() if obj is self._browser else obj.stop())
            except Exception:
                logger.exception("browser worker shutdown failed")
        self._page = self._browser = self._pw = None


async def start_worker(url: str, granted_hosts: frozenset[str]) -> BrowserWorker:
    worker = BrowserWorker(url, granted_hosts)
    await worker.start()
    return worker
