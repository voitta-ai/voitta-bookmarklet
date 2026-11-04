"""Connector registry — one entry per ``mcp_servers[]`` declaration.

A connector is the materialised handle for a manifest entry: it knows
where the server lives (via a settings key), how to read the bearer
token (also via a settings key), what host_pattern gates the tools,
and the synthetic ToolSpec names it owns.

Lifecycle:

* Plugin loader calls :func:`register_connector` once per declaration
  it finds in ``manifest.json``. No network at this point.
* The first call to :func:`refresh_all` (FastAPI startup, or explicit
  ``POST /api/plugins/<name>/refresh``) opens the current user's effective
  catalog bindings and synthesises ToolSpecs into an identity-scoped overlay.
* Subsequent refreshes re-list and replace; we remove only the
  connector's own synthesised tools so we don't disturb local Python
  tools the plugin also registered.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from app.services import user_settings
from app.tools.registry import ToolSpec, registry as _tool_registry

from . import client as mcp_client
from .tool_adapter import synth_tool_spec

_logger = logging.getLogger(__name__)


# -- manifest decl -----------------------------------------------------------


@dataclass
class MCPServerDecl:
    """Frozen interpretation of one ``manifest.mcp_servers[]`` entry.

    The plugin loader builds these from raw manifest JSON; the registry
    materialises a runtime :class:`MCPConnector` from each.
    """

    plugin_name: str
    id: str                          # connector id within the plugin
    url_setting: str | None          # dot-path into the settings blob
    token_setting: str | None        # dot-path; None means no auth needed
    tool_prefix: str
    expose_tools: list[str] | None   # None / "*" → all; else allowlist
    host_patterns: list[str]
    # Dot-path to a ``{host: token}`` map for connectors that talk to
    # several instances (url_template) — each instance mints its own
    # keys, so the bearer is matched to the host of the URL being
    # called. ``token_setting`` doubles as the any-host fallback.
    token_map_setting: str | None = None
    # Page-host-derived endpoint, e.g. ``"{host}/mcp"``. ``{host}`` is
    # replaced with the bookmarklet page's host (port included) at call
    # time, so the same plugin can talk to whichever instance the user
    # is on (https://enterprise.voitta.ai/mcp on the portal,
    # http://127.0.0.1:8756/mcp on a local install). At least one of
    # ``url_setting`` / ``url_template`` must be declared; an explicit
    # settings URL wins over the template when both resolve.
    url_template: str | None = None
    # Fixed endpoint baked into the manifest, for connectors that talk to
    # one vendor-hosted server rather than a per-instance deployment
    # (e.g. Runpod's https://mcp.getrunpod.io/). Neither of the other two
    # fits such a server: ``url_template`` requires ``{host}``, and
    # ``url_setting`` would make the user type a URL that never varies.
    # A user-set ``url_setting`` still wins, so it stays overridable.
    url: str | None = None
    # Future: transport variants. Today only "streamable-http".
    transport: str = "streamable-http"
    # Set only on a user-scoped catalog binding, never on manifest defaults.
    catalog_id: str | None = None

    @classmethod
    def from_manifest_entry(
        cls,
        *,
        plugin_name: str,
        host_patterns: list[str],
        raw: dict[str, Any],
    ) -> "MCPServerDecl":
        """Parse one ``mcp_servers[]`` entry. Raises on malformed input.

        The plugin loader catches the exception and logs it; the plugin's
        other tools still register, only this connector is skipped. We
        prefer hard validation here over silent fall-throughs so a typo'd
        manifest is visible at startup, not three chat turns later.
        """
        if not isinstance(raw, dict):
            raise ValueError("mcp_servers entry must be an object")
        cid = str(raw.get("id") or "").strip()
        if not cid:
            raise ValueError("mcp_servers entry missing required field 'id'")
        url_setting = str(raw.get("url_setting") or "").strip() or None
        url_template = str(raw.get("url_template") or "").strip() or None
        url_fixed = str(raw.get("url") or "").strip() or None
        if not url_setting and not url_template and not url_fixed:
            raise ValueError(
                f"mcp_servers[{cid}] needs 'url', 'url_setting' or 'url_template'"
            )
        if url_template and "{host}" not in url_template:
            raise ValueError(
                f"mcp_servers[{cid}].url_template must contain '{{host}}'"
            )
        if url_fixed and "://" not in url_fixed:
            raise ValueError(
                f"mcp_servers[{cid}].url must be absolute (got {url_fixed!r})"
            )
        auth = raw.get("auth") or {}
        token_setting: str | None = None
        token_map_setting: str | None = None
        if isinstance(auth, dict) and auth.get("type") == "bearer":
            token_setting = str(auth.get("token_setting") or "").strip() or None
            token_map_setting = (
                str(auth.get("token_map_setting") or "").strip() or None
            )

        tool_prefix = str(raw.get("tool_prefix") or f"{plugin_name}_")
        expose = raw.get("expose_tools")
        expose_tools: list[str] | None
        if expose is None or expose == "*":
            expose_tools = None
        elif isinstance(expose, list):
            expose_tools = [str(t) for t in expose if isinstance(t, str)]
        else:
            raise ValueError(
                f"mcp_servers[{cid}].expose_tools must be '*', omitted, or a list"
            )

        transport = str(raw.get("transport") or "streamable-http")
        if transport != "streamable-http":
            raise ValueError(
                f"mcp_servers[{cid}].transport={transport!r} not supported "
                "(only 'streamable-http' today)"
            )

        return cls(
            plugin_name=plugin_name,
            id=cid,
            url_setting=url_setting,
            token_setting=token_setting,
            tool_prefix=tool_prefix,
            expose_tools=expose_tools,
            host_patterns=host_patterns,
            token_map_setting=token_map_setting,
            url_template=url_template,
            url=url_fixed,
            transport=transport,
        )


# -- runtime connector -------------------------------------------------------


@dataclass
class EndpointStatus:
    """Last probe result for ONE endpoint of a connector.

    Template connectors (``url_template``) fan out to several instances
    — one per activation host — and each is its own connection with its
    own status and tool list.
    """

    url: str
    # "ok" | "unauth" | "unreachable"
    status: str
    last_error: str | None = None
    tool_names: list[str] = field(default_factory=list)
    # Raw mcp.types.Tool objects from this endpoint's last successful
    # probe — kept so a single-endpoint refresh can re-merge the
    # registration without re-probing the others. Not serialised.
    remote_tools: list[Any] = field(default_factory=list, compare=False)


@dataclass
class MCPConnector:
    """Runtime state for one MCP server connection.

    Holds the manifest declaration plus the last probe result so the
    settings UI can render an at-a-glance status badge (connected /
    unauthorized / unreachable / not-configured) without re-probing on
    every page render.
    """

    decl: MCPServerDecl
    # Aggregate status: "unknown" | "ok" (≥1 endpoint ok) | "unauth" |
    # "unreachable" | "not_configured"
    status: str = "unknown"
    last_error: str | None = None
    tool_names: list[str] = field(default_factory=list)
    # Per-endpoint probe results from the last refresh, in candidate
    # order (explicit URL, manifest hosts, user extra hosts).
    endpoints: list[EndpointStatus] = field(default_factory=list)
    # Names of synthetic ToolSpecs we own in this user's tool overlay.
    # Tracked separately so a refresh can remove only ours.
    owned_tool_names: set[str] = field(default_factory=set)
    # The first endpoint the last refresh reached. Doubles as the
    # fallback endpoint for host-less callers (the ``vre://`` ref
    # resolver runs inside script execution with no page in scope).
    active_url: str | None = None
    owner: str | None = None
    catalog_signature: str | None = None


# -- module-global registry --------------------------------------------------

# Manifest prototypes populated by the plugin loader at startup. Runtime
# states live in catalog._STATES, never here. Keyed by
# ``"<plugin_name>:<connector_id>"``.
MCP_CONNECTORS: dict[str, MCPConnector] = {}


def _key(plugin_name: str, connector_id: str) -> str:
    return f"{plugin_name}:{connector_id}"


def register_connector(decl: MCPServerDecl) -> MCPConnector:
    """Register a connector from a manifest declaration. Idempotent.

    Called by the plugin loader during ``_import_plugin``. Doesn't
    open any connections — just records the declaration so a later
    :func:`refresh_all` can act on it.
    """
    k = _key(decl.plugin_name, decl.id)
    existing = MCP_CONNECTORS.get(k)
    if existing is not None:
        # Re-registration replaces the declaration but preserves the
        # owned-tools set so a plugin reload (future feature) can swap
        # them atomically. Today plugins only register once at startup.
        existing.decl = decl
        return existing
    conn = MCPConnector(decl=decl)
    MCP_CONNECTORS[k] = conn
    return conn


def get_connector(plugin_name: str, connector_id: str) -> MCPConnector | None:
    return next((c for c in list_connectors(plugin_name) if c.decl.id == connector_id), None)


def list_connectors(plugin_name: str | None = None) -> list[MCPConnector]:
    """List the current user's effective catalog bindings."""
    from . import catalog
    return catalog.list_connectors(plugin_name)


def unregister_plugin(plugin_name: str) -> None:
    """Drop every connector belonging to a plugin (used by hot-reload paths).

    Not called at startup; kept for forward compatibility when we add a
    "reload plugin" admin button. Removes the synthesised ToolSpecs from
    the global registry too.
    """
    victims = [k for k, c in MCP_CONNECTORS.items() if c.decl.plugin_name == plugin_name]
    for k in victims:
        conn = MCP_CONNECTORS.pop(k)
        _drop_owned_tools(conn)


# -- settings access ---------------------------------------------------------


def _dotted_get(blob: dict[str, Any], path: str) -> Any:
    """Walk ``a.b.c`` through nested dicts; return None for misses.

    Manifest entries reference settings via dot-paths like
    ``plugins.voitta-enterprise.mcp.url``. The settings file itself
    stores them as the natural nested JSON shape — no flattening at
    the storage layer. This helper is the runtime bridge.
    """
    cur: Any = blob
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
        if cur is None:
            return None
    return cur


def _read_url(decl: MCPServerDecl) -> str | None:
    """Read the configured URL out of the live settings file.

    Returns None when the manifest declares no ``url_setting`` or the
    user hasn't filled the field in. Callers must handle that case
    (the synthesised tool handler does, returning an
    ``mcp_not_configured`` error).
    """
    if not decl.url_setting:
        return None
    try:
        val = _dotted_get(user_settings.read(), decl.url_setting)
    except Exception:
        return None
    return (val or None) if isinstance(val, str) else None


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def _fill_template(template: str, host: str) -> str:
    """Substitute ``{host}`` and prepend a scheme if the template has none.

    Loopback hosts get ``http`` (local dev servers rarely carry certs);
    everything else gets ``https``. A template that already embeds a
    scheme (``https://{host}/mcp``) is used verbatim.
    """
    url = template.replace("{host}", host.strip().rstrip("/"))
    if "://" in url:
        return url
    hostname = host.split(":", 1)[0].lower()
    scheme = "http" if hostname in _LOOPBACK_HOSTS else "https"
    return f"{scheme}://{url}"


def endpoint_for(conn: MCPConnector, host: str | None = None) -> str | None:
    """Resolve the connector's endpoint for a call originating on ``host``.

    Precedence: explicit settings URL → manifest's fixed URL → template
    filled with the page host → the URL the last successful refresh used.
    The last leg keeps host-less callers working (the vre:// resolver,
    background jobs).
    """
    if conn.decl.catalog_id:
        from . import catalog
        if not catalog.connector_current(conn):
            return None
    explicit = _read_url(conn.decl)
    if explicit:
        return explicit
    if conn.decl.url:
        return conn.decl.url
    if conn.decl.url_template and host:
        return _fill_template(conn.decl.url_template, host)
    return conn.active_url


def _candidate_urls(decl: MCPServerDecl) -> list[str]:
    """Endpoints a refresh should try, in order.

    Explicit settings URL first, then the manifest's fixed ``url``, then
    the template applied to each activation host: manifest
    ``host_patterns`` (skipping wildcards) followed by the user's extra
    hosts (Settings → Plugins).
    """
    urls: list[str] = []
    explicit = _read_url(decl)
    if explicit:
        urls.append(explicit)
    if decl.url and decl.url not in urls:
        urls.append(decl.url)
    if decl.url_template:
        hosts = [p for p in decl.host_patterns if p and "*" not in p]
        try:
            from app.services import host_activation
            hosts += host_activation.extra_hosts_map().get(decl.plugin_name, [])
        except Exception:
            _logger.exception("extra-hosts lookup failed during mcp refresh")
        for h in hosts:
            url = _fill_template(decl.url_template, h)
            if url not in urls:
                urls.append(url)
    return urls


def _read_token(decl: MCPServerDecl) -> str | None:
    """Read the configured any-host bearer token. None means "no auth".

    Local-dev mode on the server side (``VOITTA_SINGLE_USER`` etc.)
    accepts unauthenticated MCP calls, so an empty token is a
    legitimate value here — we don't synthesise a placeholder.
    """
    if not decl.token_setting:
        return None
    try:
        val = _dotted_get(user_settings.read(), decl.token_setting)
    except Exception:
        return None
    return (val or None) if isinstance(val, str) else None


def _host_of(url_or_host: str) -> str:
    """``"https://x.y:8756/mcp"`` / ``"x.y:8756"`` → ``"x.y:8756"``."""
    s = url_or_host.strip()
    if "://" in s:
        s = s.split("://", 1)[1]
    return s.split("/", 1)[0].lower()


def token_for(decl: MCPServerDecl, url_or_host: str | None = None) -> str | None:
    """Resolve the bearer token for a call to ``url_or_host``.

    Per-host map first (``token_map_setting`` — entries are host strings
    like ``"enterprise.voitta.ai"`` or ``"127.0.0.1:8756"``; an entry
    with a port matches only that port, a bare hostname suffix-matches),
    then the legacy any-host ``token_setting``. The token follows the
    host of the URL being CALLED, not the page host — each instance
    mints its own keys.
    """
    from . import catalog
    overridden, token = catalog.token_override(decl)
    if overridden:
        return token
    if decl.token_map_setting and url_or_host:
        try:
            mapping = _dotted_get(user_settings.read(), decl.token_map_setting)
        except Exception:
            mapping = None
        if isinstance(mapping, dict):
            host = _host_of(url_or_host)
            exact = mapping.get(host)
            if isinstance(exact, str) and exact:
                return exact
            from app.services import host_activation
            for entry, key in mapping.items():
                if not isinstance(key, str) or not key:
                    continue
                if host_activation.matches(host, [str(entry).lower()]):
                    return key
    return _read_token(decl)


# -- refresh / tool synthesis ------------------------------------------------


def _drop_owned_tools(conn: MCPConnector) -> None:
    """Remove every ToolSpec this connector previously synthesised.

    Iterates the underlying dict directly because ``ToolRegistry``
    today has no remove() — kept the abstraction surface minimal.
    Touching ``_tools`` here is intentional and bounded to this file.
    """
    from . import catalog
    owned = catalog._TOOLS.get(conn.owner, {})
    for name in list(conn.owned_tool_names):
        owned.pop(name, None)
    conn.owned_tool_names.clear()
    conn.tool_names = []


def _is_exposed(decl: MCPServerDecl, remote_name: str) -> bool:
    if decl.expose_tools is None:
        return True
    return remote_name in decl.expose_tools


def _host_pattern_for(decl: MCPServerDecl) -> str | list[str] | None:
    """Pick the host_pattern shape ToolSpec wants.

    Single-host plugins get a bare string (cheaper to serialise + match
    against). Multi-host plugins get the full list; the registry's
    matcher OR's them.
    """
    pats = [p for p in decl.host_patterns if isinstance(p, str) and p]
    if not pats or "*" in pats:
        return None
    return pats[0] if len(pats) == 1 else pats


async def _probe_endpoint(decl: MCPServerDecl, url: str) -> EndpointStatus:
    """List tools from ONE endpoint, with that endpoint's own token."""
    try:
        listed = await mcp_client.list_tools(url, token_for(decl, url))
    except Exception as exc:
        msg = str(exc) or type(exc).__name__
        status = "unauth" if _is_auth_error(exc, msg) else "unreachable"
        # Remote exceptions can echo Authorization headers. Status details must
        # not disclose credentials in the API or logs.
        safe_error = "MCP authentication failed" if status == "unauth" else "MCP endpoint unavailable"
        _logger.warning("mcp probe failed: connector=%s:%s status=%s", decl.plugin_name, decl.id, status)
        return EndpointStatus(url=url, status=status, last_error=safe_error if decl.catalog_id else msg)
    exposed = [t for t in listed if _is_exposed(decl, t.name)]
    return EndpointStatus(
        url=url,
        status="ok",
        tool_names=sorted(f"{decl.tool_prefix}{t.name}" for t in exposed),
        remote_tools=exposed,
    )


def _finalize(conn: MCPConnector) -> MCPConnector:
    """Derive aggregate status + (re)register tools from ``conn.endpoints``.

    Tools are merged by remote name across the ok endpoints: the local
    name is the same either way and the handler routes to the right
    instance per page host at call time.
    """
    from . import catalog
    if not catalog.connector_current(conn):
        _drop_owned_tools(conn)
        conn.status = "disabled"
        return conn
    decl = conn.decl
    ok_eps = [e for e in conn.endpoints if e.status == "ok"]
    if not ok_eps:
        if any(e.status == "unauth" for e in conn.endpoints):
            conn.status = "unauth"
            _drop_owned_tools(conn)
        else:
            conn.status = "unreachable"
            # Keep prior tools so a brief outage doesn't pull working
            # tools out from under an in-progress chat. The next
            # successful refresh will replace them.
        conn.last_error = "; ".join(
            f"{e.url}: {e.last_error}" for e in conn.endpoints if e.last_error
        )
        _logger.warning(
            "mcp refresh failed: plugin=%s connector=%s errors=%s",
            decl.plugin_name, decl.id, conn.last_error,
        )
        return conn

    merged: dict[str, Any] = {}
    for e in ok_eps:
        for t in e.remote_tools:
            merged.setdefault(t.name, t)

    # ≥1 endpoint up — drop the old, register the merged set. The
    # handler resolves the endpoint per call from the page host
    # (template connectors follow the bookmarklet), falling back to the
    # first reachable URL from this refresh.
    _drop_owned_tools(conn)
    conn.active_url = ok_eps[0].url
    url_provider: Callable[[str | None], str | None] = (
        lambda host=None, c=conn: endpoint_for(c, host)
    )
    token_provider: Callable[[str | None], str | None] = (
        lambda url=None, d=decl: token_for(d, url)
    )
    host_pattern = _host_pattern_for(decl)

    visibility_check: Callable[[], bool] = lambda c=conn: c.status == "ok" and catalog.connector_current(c)

    registered: list[str] = []
    for t in merged.values():
        spec: ToolSpec = synth_tool_spec(
            connector_id=f"{decl.plugin_name}:{decl.id}",
            remote_tool=t,
            tool_prefix=decl.tool_prefix,
            url_provider=url_provider,
            token_provider=token_provider,
            host_pattern=host_pattern,
            visibility_check=visibility_check,
            call_check=lambda ctx, c=conn: (
                catalog.connector_current(c) and c.status == "ok"
                and (ctx.email is None or ctx.email == catalog.identity())
                and (not c.decl.catalog_id or catalog.allowed_host(c, ctx.host))
            ),
        )
        # Tag ownership so user-added activation hosts
        # (plugins.<name>.extra_hosts) can widen the host gate.
        spec.plugin_name = decl.plugin_name
        if _tool_registry.get(spec.name) is not None:
            _logger.warning("mcp tool name collision: connector=%s name=%s — skipped", decl.id, spec.name)
            continue
        catalog.tools()[spec.name] = spec
        registered.append(spec.name)

    conn.owned_tool_names = set(registered)
    conn.tool_names = registered
    conn.status = "ok"
    conn.last_error = None
    _logger.info(
        "mcp refresh ok: plugin=%s connector=%s host_pattern=%r tools=%d: %s",
        decl.plugin_name, decl.id, host_pattern, len(registered), registered,
    )
    return conn


async def refresh_one(conn: MCPConnector) -> MCPConnector:
    """Probe every endpoint of one connector, replace its synthesised tools.

    Endpoint results are built into a LOCAL list and assigned at the end
    — a concurrent refresh (startup refresh_all racing a user click)
    must not interleave appends into the shared list (that's how
    duplicate rows happen).
    """
    from . import catalog
    if not catalog.connector_current(conn):
        _drop_owned_tools(conn)
        conn.status = "disabled"
        conn.endpoints = []
        conn.active_url = None
        return conn
    decl = conn.decl
    candidates = _candidate_urls(decl)
    _logger.info(
        "refresh_one: plugin=%s connector=%s candidates=%r",
        decl.plugin_name, decl.id, candidates,
    )
    if not candidates:
        conn.status = "not_configured"
        conn.last_error = None
        conn.active_url = None
        conn.endpoints = []
        _drop_owned_tools(conn)
        return conn

    probed = [await _probe_endpoint(decl, url) for url in candidates]
    conn.endpoints = probed
    return _finalize(conn)


async def refresh_endpoint(conn: MCPConnector, host: str) -> MCPConnector:
    """Re-probe ONLY the endpoint(s) for ``host``; keep the others as-is.

    Backs the per-row Connect/Refresh button in the settings card. The
    other endpoints' last probe results (including their remote tool
    lists) stay untouched and the merged registration is rebuilt from
    the union.
    """
    from . import catalog
    if not catalog.connector_current(conn):
        return await refresh_one(conn)
    decl = conn.decl
    want = _host_of(host)
    targets = [u for u in _candidate_urls(decl) if _host_of(u) == want]
    if not targets and decl.url_template:
        targets = [_fill_template(decl.url_template, host)]
    if not targets:
        return conn

    probed = {url: await _probe_endpoint(decl, url) for url in targets}
    next_eps: list[EndpointStatus] = []
    for e in conn.endpoints:
        next_eps.append(probed.pop(e.url, e))
    next_eps.extend(probed.values())
    conn.endpoints = next_eps
    return _finalize(conn)


async def refresh_all() -> list[MCPConnector]:
    """Probe every registered connector. Errors are absorbed per-connector.

    Returns the connector list in registration order so callers can
    surface per-connector status in the UI.
    """
    out: list[MCPConnector] = []
    for conn in list_connectors():
        out.append(await refresh_one(conn))
    return out


def _is_auth_error(exc: Exception, msg: str) -> bool:
    """Heuristic: does this exception look like a 401/403?

    fastmcp / httpx surface HTTP errors with the status code in the
    string but no easily-introspectable status attribute. A substring
    match is good enough for status-badge purposes; the precise error
    text always shows in the UI regardless of classification.
    """
    msg = msg.lower()
    if "401" in msg or "unauthorized" in msg:
        return True
    if "403" in msg or "forbidden" in msg:
        return True
    return False
