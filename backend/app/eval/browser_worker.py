"""Unattended browser worker: the eval runner's :class:`BrowserTransport`.

One worker per eval session. It launches its own headless Chromium with a
fresh, throwaway profile (never a user profile), loads the session's fixture
page, and refuses every request outside the fixture's exact origin (scheme,
host and port), so a script run through it cannot navigate or fetch
elsewhere, including other services on the same host. Deadlines are enforced
here, not by the page: when one passes, the page and its profile are thrown
away and the fixture is loaded again, so a runaway script cannot outlive its
call.

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
# async IIFE via indirect eval, console captured. No page-side timeout race:
# a race only stops waiting, the script keeps running. The worker enforces
# await_ms itself and resets the page when it passes.
_EVAL_JS = """
async (js) => {
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
    const result = await indirectEval(`(async () => {\\n${js}\\n})()`);
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


def origin(url: str) -> str:
    """``scheme://host:port`` with the default port made explicit."""
    parts = urlsplit(url)
    port = parts.port or {"http": 80, "https": 443}.get(parts.scheme, 0)
    return f"{parts.scheme}://{(parts.hostname or '').lower()}:{port}"


class BrowserWorker:
    def __init__(self, url: str) -> None:
        self.url = url
        self.origin = origin(url)
        self._pw: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self.blocked: list[str] = []

    async def start(self) -> None:
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        try:
            self._browser = await self._pw.chromium.launch(headless=True)
            await self._load_fixture()
        except BaseException:
            await self.close()
            raise

    async def _load_fixture(self) -> None:
        # new_context() is an in-memory, throwaway profile. Service workers
        # are blocked because their requests bypass the route guard.
        self._context = await self._browser.new_context(service_workers="block")
        await self._context.route("**/*", self._guard)
        self._page = await self._context.new_page()
        await self._page.goto(self.url, wait_until="load", timeout=15_000)

    async def _reset(self) -> None:
        """Drop the page and profile (stopping any script still running in
        them) and load the fixture into a fresh one."""
        old, self._context, self._page = self._context, None, None
        try:
            await old.close()
        except Exception:
            logger.exception("browser worker context close failed")
        await self._load_fixture()

    async def _guard(self, route: Any) -> None:
        url = route.request.url
        scheme = urlsplit(url).scheme
        if scheme in ("data", "blob", "about") or origin(url) == self.origin:
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
            timeout_ms = min(timeout_ms, int(args.get("await_ms") or 30_000))
            op = self._page.evaluate(_EVAL_JS, js)
        else:
            raise BrowserToolError(
                "tool_unavailable",
                f"browser primitive {name!r} is not supported by the eval worker",
            )
        task = asyncio.ensure_future(op)
        try:
            res = await asyncio.wait_for(asyncio.shield(task), timeout=timeout_ms / 1000)
        except asyncio.TimeoutError as exc:
            # The script may still be running in the page: throw the page away.
            task.add_done_callback(lambda t: t.cancelled() or t.exception())
            await self._reset()
            raise BrowserToolError(
                "timeout",
                f"browser primitive {name!r} gave no result within {timeout_ms}ms; "
                "the page was reset to a fresh copy of the fixture",
            ) from exc
        except BrowserToolError:
            raise
        except Exception as exc:  # noqa: BLE001 — playwright raises various types
            raise BrowserToolError("dispatch_failed", f"browser primitive {name!r} failed: {exc}") from exc
        return {"title": res} if name == "get_page_title" else res

    async def close(self) -> None:
        self._context = None
        for obj in (self._browser, self._pw):
            if obj is None:
                continue
            try:
                await (obj.close() if obj is self._browser else obj.stop())
            except Exception:
                logger.exception("browser worker shutdown failed")
        self._page = self._browser = self._pw = None


async def start_worker(url: str) -> BrowserWorker:
    worker = BrowserWorker(url)
    await worker.start()
    return worker
