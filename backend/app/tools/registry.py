"""Registry of LLM-facing tools.

A :class:`ToolSpec` describes one tool: name, description, JSON-schema
input shape, and an async handler. The handler receives parsed args
and a :class:`ToolCtx` carrying request-scoped state (most importantly
``session_id`` and ``host`` — used by hybrid tools that round-trip to
the browser via :func:`app.tools.browser.call_browser`).

Two ``side`` values, both dispatched identically (the registry just
calls the handler):

* ``"server"`` — handler runs in-process and returns a result.
* ``"hybrid"`` — handler runs in-process but typically calls
  ``call_browser(...)`` to delegate work to a browser-side primitive.
  Same dispatch, different gating semantics for the FE.

The registry singleton is exposed as :data:`registry`.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

logger = logging.getLogger(__name__)


@dataclass
class ToolCtx:
    """Request-scoped state passed to tool handlers.

    ``extras`` is a free-form per-request payload plugins can read /
    write without core needing to add a field. Keys are
    plugin-namespaced by convention (e.g. ``"voitta_google.tenant"``).
    """

    session_id: str | None = None
    host: str | None = None
    # Authenticated user email in server mode (None on desktop/dev). Identity
    # for per-user data isolation; see app.services.current_user.
    email: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)
    # How hybrid tools reach the page (app.tools.browser.BrowserTransport).
    # None = the Chainlit round-trip to the user's bookmarklet.
    browser: Any = None


@dataclass
class ToolResult:
    ok: bool
    result: Any = None
    error: dict[str, Any] | None = None
    latency_ms: int = 0


ToolHandler = Callable[[dict[str, Any], ToolCtx], Awaitable[Any]]


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler
    side: Literal["server", "hybrid"] = "server"
    # Strict-suffix host gate. ``None`` = visible everywhere. The
    # plugin loader back-fills this from manifest ``host_patterns`` on
    # plugin-contributed specs that didn't set their own (per-tool
    # values win over the manifest default).
    host_pattern: str | list[str] | None = None
    # Set True for core tools that must be visible on every host.
    # The plugin loader's back-fill skips specs where this is True so
    # a plugin's host_patterns can't accidentally gate a global tool.
    global_tool: bool = False
    # Runtime gate: if set, the tool only appears in the LLM's list
    # when the callable returns True. Used for state the LLM can't
    # manipulate (e.g. "Drive tools only show when OAuth is connected").
    # Called on every turn — keep cheap.
    visibility_check: Callable[[], bool] | None = None
    # Optional runtime SUFFIX for the description, evaluated at the same
    # per-turn moment as visibility_check (schemas_for_host). Used for
    # state the static description can't know at import time (e.g. the
    # roster of connected Google accounts). Returns "" for "nothing to
    # add"; exceptions fall back to the static description alone.
    # Called on every turn — keep cheap.
    dynamic_description: Callable[[], str] | None = None
    # Owning plugin, back-filled by the plugin loader / MCP refresh.
    # Lets user-added activation hosts (Settings → Plugins, stored at
    # ``plugins.<name>.extra_hosts``) widen this spec's host gate
    # without touching ``host_pattern``.
    plugin_name: str | None = None
    # Wall-clock cap for one call, in seconds, for dispatch paths that bound
    # tools (the agent-sdk bridge does; the API-key loop does not). ``None``
    # means the path's default. Set it on tools that legitimately outlive
    # that default — ask_user_question waits on a human, not a computation.
    timeout_s: float | None = None


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        # Collision policy: log + skip rather than raise. Raising aborts
        # the plugin's whole import, dropping every sibling tool the
        # plugin also wanted to contribute. Skip-and-keep preserves the
        # rest of the plugin's surface.
        prior = self._tools.get(spec.name)
        if prior is not None:
            logger.warning(
                "tool %r already registered by %s.%s — keeping prior, "
                "skipping new spec from %s.%s",
                spec.name,
                getattr(prior.handler, "__module__", "?"),
                getattr(prior.handler, "__qualname__", "?"),
                getattr(spec.handler, "__module__", "?"),
                getattr(spec.handler, "__qualname__", "?"),
            )
            return
        self._tools[spec.name] = spec

    def all(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def names(self) -> list[str]:
        return sorted(self._tools)

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def visible_for_host(self, host: str | None) -> list[ToolSpec]:
        """Tools whose ``host_pattern`` matches ``host`` and whose
        ``visibility_check`` (if any) returns True.

        Match rule for ``host_pattern``: STRICT suffix on the bare
        hostname. ``"ebay.com"`` matches ``ebay.com`` and
        ``www.ebay.com`` but NOT ``ebay.com.evil.com``. List of
        patterns is OR'd. ``host`` may include ``:port`` — stripped.
        User-added activation hosts (``plugins.<name>.extra_hosts``,
        port-aware) are OR'd in for specs that carry a ``plugin_name``.
        """
        hostname = (host or "").split(":", 1)[0].lower().rstrip(".")
        out: list[ToolSpec] = []
        hidden: list[str] = []
        # Lazy, one settings read per call — not one per spec.
        extra_map: dict[str, list[str]] | None = None

        def _extra_match(plugin_name: str | None) -> bool:
            nonlocal extra_map
            if not plugin_name or not host:
                return False
            try:
                from app.services import host_activation
            except Exception:
                return False
            if extra_map is None:
                try:
                    extra_map = host_activation.extra_hosts_map()
                except Exception:
                    logger.exception("extra-hosts lookup failed")
                    extra_map = {}
            return host_activation.matches(host, extra_map.get(plugin_name, []))

        for s in self._tools.values():
            if s.host_pattern is not None:
                if not hostname:
                    hidden.append(f"{s.name}(no-host)")
                    continue
                patterns = (
                    [s.host_pattern]
                    if isinstance(s.host_pattern, str)
                    else list(s.host_pattern)
                )
                matched = False
                for raw in patterns:
                    if not isinstance(raw, str) or not raw:
                        continue
                    pat = raw.lower().rstrip(".")
                    if hostname == pat or hostname.endswith("." + pat):
                        matched = True
                        break
                if not matched and _extra_match(s.plugin_name):
                    matched = True
                if not matched:
                    hidden.append(f"{s.name}(host-mismatch:{patterns}!={hostname!r})")
                    continue
            if s.visibility_check is not None:
                try:
                    if not s.visibility_check():
                        hidden.append(f"{s.name}(visibility-false)")
                        continue
                except Exception:
                    logger.exception("visibility_check for %r raised", s.name)
                    hidden.append(f"{s.name}(visibility-exc)")
                    continue
            out.append(s)
        if hidden:
            logger.debug("visible_for_host(%r): hidden %d tools: %s", host, len(hidden), hidden)
        return out

    def schemas_for_host(self, host: str | None) -> list[dict[str, Any]]:
        """Anthropic-shaped tool list filtered by host + visibility."""
        return [
            {
                "name": s.name,
                "description": self._describe(s),
                "input_schema": s.input_schema,
            }
            for s in self.visible_for_host(host)
        ]

    @staticmethod
    def _describe(s: ToolSpec) -> str:
        """Static description plus the dynamic suffix (if any). A raising
        suffix callable degrades to the static description — a broken
        roster must never hide a working tool."""
        if s.dynamic_description is None:
            return s.description
        try:
            return s.description + (s.dynamic_description() or "")
        except Exception:
            logger.exception("dynamic_description for %r raised", s.name)
            return s.description

    async def dispatch(
        self, name: str, args: dict[str, Any], ctx: ToolCtx
    ) -> ToolResult:
        t0 = time.perf_counter()
        spec = self._tools.get(name)
        if spec is None:
            return ToolResult(
                ok=False,
                error={"kind": "unknown_tool", "message": f"unknown tool {name!r}"},
                latency_ms=0,
            )
        # Activity tracking — feeds the menu-bar status colour. Begin/
        # end around the handler so the glyph reflects what's running.
        # Lazy import keeps this module testable in environments without
        # the desktop layer.
        try:
            from app import activity as _act
            token = _act.begin(_act.classify(name), detail=name)
        except Exception:
            token = None
        try:
            # All handlers are async (args, ctx). Validate up front so a
            # mis-shaped handler fails loudly at registration-discovery
            # rather than at call time.
            result = await spec.handler(args, ctx)
            return ToolResult(
                ok=True,
                result=result,
                latency_ms=int((time.perf_counter() - t0) * 1000),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # BrowserToolError carries a structured envelope; everything
            # else gets a generic shape.
            err: dict[str, Any] = {
                "kind": getattr(exc, "kind", type(exc).__name__),
                "message": str(exc),
            }
            details = getattr(exc, "details", None)
            if details is not None:
                err["details"] = details
            logger.exception("tool %s dispatch failed", name)
            return ToolResult(
                ok=False,
                error=err,
                latency_ms=int((time.perf_counter() - t0) * 1000),
            )
        finally:
            if token is not None:
                try:
                    from app import activity as _act
                    _act.end(token)
                except Exception:
                    pass

    def reset_for_tests(self) -> None:
        self._tools.clear()


# Module-level singleton — what plugins (and core tools) import.
registry = ToolRegistry()


# Back-compat helpers used by the existing plugin loader (which
# snapshots the registry's keys before/after a plugin import to
# back-fill host_pattern on its newly-added specs).
def _registry_keys() -> set[str]:
    return set(registry._tools.keys())


def _back_fill_host_pattern(
    new_keys: set[str], pattern: str | list[str]
) -> int:
    """For each newly-added spec whose ``host_pattern is None``, set it
    to the manifest's ``host_patterns``. Returns the number of specs
    updated."""
    applied = 0
    for name in new_keys:
        spec = registry._tools.get(name)
        if spec is not None and spec.host_pattern is None and not spec.global_tool:
            spec.host_pattern = pattern
            applied += 1
    return applied
