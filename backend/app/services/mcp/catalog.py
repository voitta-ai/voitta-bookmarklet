"""User-owned HTTP MCP catalog and plugin bindings.

Token precedence: binding override > explicit server token (including an
explicit empty token) > builtin legacy per-host token > legacy fallback.
Legacy credentials only belong to the original binding, and are not forwarded
when its URL is changed in the catalog. Additional bindings inherit only the
explicit server token. Clearing a binding token restores server inheritance.
Clearing a server token suppresses legacy fallback without deleting old keys.

Manifest declarations are immutable process-wide defaults. Runtime connectors
and tools live in identity-scoped overlays; no endpoint or schema discovered
for one user is registered in the shared Python tool registry.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit

from app.services import current_user, host_activation, user_settings

SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,95}$")
SAFE_PREFIX = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")

# Keyed by authenticated identity (None is desktop), never by page host.
_STATES: dict[str | None, dict[str, Any]] = {}
_TOOLS: dict[str | None, dict[str, Any]] = {}


def identity() -> str | None:
    return current_user.get_current_email()


def tools() -> dict[str, Any]:
    return _TOOLS.setdefault(identity(), {})


def builtin_id(decl) -> str:
    raw = f"{decl.plugin_name}--{decl.id}"
    if SAFE_ID.fullmatch(raw):
        return raw
    safe = re.sub(r"[^a-z0-9_-]", "-", raw.lower())[:80].strip("-") or "server"
    return f"{safe}-{hashlib.sha256(raw.encode()).hexdigest()[:10]}"


def _object(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def servers(blob: dict | None = None) -> dict[str, dict]:
    from .registry import MCP_CONNECTORS, _dotted_get

    blob = user_settings.read() if blob is None else blob
    saved = _object(_object(blob.get("mcp")).get("servers"))
    out = {}
    for prototype in MCP_CONNECTORS.values():
        d = prototype.decl
        sid = builtin_id(d)
        legacy_url = _dotted_get(blob, d.url_setting) if d.url_setting else None
        out[sid] = {
            "id": sid, "name": f"{d.plugin_name} / {d.id}",
            "url": legacy_url or d.url or "", "url_template": d.url_template or "",
            "tool_prefix": d.tool_prefix, "transport": d.transport,
            "enabled": True, "builtin": True, "_original": d,
        }
    for sid, raw in saved.items():
        if not SAFE_ID.fullmatch(sid) or not isinstance(raw, dict):
            continue
        entry = out.setdefault(sid, {
            "id": sid, "name": sid, "url": "", "url_template": "",
            "tool_prefix": "mcp_", "transport": "streamable-http",
            "enabled": True, "builtin": False,
        })
        original_urls = (entry.get("url") or "", entry.get("url_template") or "")
        entry.update({k: raw[k] for k in ("name", "url", "url_template", "tool_prefix", "enabled", "token") if k in raw})
        entry["_url_overridden"] = (entry.get("url") or "", entry.get("url_template") or "") != original_urls
    return out


def bindings(server: dict, blob: dict) -> dict[str, dict]:
    from app.plugins import all_plugins

    original = server.get("_original")
    names = {p.name for p in all_plugins()}
    if original:
        names.add(original.plugin_name)
    out = {}
    for name in sorted(names):
        raw = _object(_object(_object(blob.get("plugins")).get(name)).get("mcp_bindings")).get(server["id"])
        is_original = bool(original and original.plugin_name == name)
        if raw is not None or is_original:
            conf = _object(raw)
            out[name] = {"enabled": conf.get("enabled", is_original), **({"token": conf["token"]} if conf.get("token") else {})}
    return out


def _signature(server: dict, plugin: str, binding: dict, blob: dict) -> str:
    # Tokens are resolved live and deliberately excluded: rotation needs no
    # schema refresh. URLs, activation hosts and enablement invalidate schemas.
    data = {k: server.get(k) for k in ("id", "url", "url_template", "tool_prefix", "enabled")}
    data.update(plugin=plugin, binding_enabled=binding.get("enabled"),
                extra_hosts=_object(_object(blob.get("plugins")).get(plugin)).get("extra_hosts", []))
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def _effective_decl(server: dict, plugin: str):
    from app.plugins import all_plugins
    from .registry import MCPServerDecl

    original = server.get("_original")
    own = bool(original and original.plugin_name == plugin)
    patterns = next((list(p.host_patterns) for p in all_plugins() if p.name == plugin),
                    list(original.host_patterns) if own else [])
    prefix = server["tool_prefix"]
    if not own:
        label = re.sub(r"[^a-zA-Z0-9_]", "_", plugin)
        suffix = hashlib.sha256(f"{server['id']}:{plugin}".encode()).hexdigest()[:8]
        prefix = f"{prefix}{label}_{suffix}_"
    d = replace(original) if own else MCPServerDecl(
        plugin_name=plugin, id=server["id"], url_setting=None,
        token_setting=None, tool_prefix=prefix, expose_tools=None,
        host_patterns=patterns,
    )
    d.plugin_name = plugin
    d.tool_prefix = prefix
    d.host_patterns = patterns
    d.url_setting = None
    d.url = server.get("url") or None
    d.url_template = server.get("url_template") or None
    # A fixed catalog URL is authoritative; do not also probe old template hosts.
    if d.url:
        d.url_template = None
    d.catalog_id = server["id"]
    if not own or server.get("_url_overridden"):
        d.token_setting = None
        d.token_map_setting = None
    return d


def list_connectors(plugin_name: str | None = None) -> list:
    from .registry import MCPConnector, _drop_owned_tools

    blob = user_settings.read()
    state = _STATES.setdefault(identity(), {})
    wanted = {}
    for server in servers(blob).values():
        for plugin, binding in bindings(server, blob).items():
            key = f"{server['id']}:{plugin}"
            sig = _signature(server, plugin, binding, blob)
            old = state.get(key)
            if old is None or old.catalog_signature != sig:
                if old is not None:
                    _drop_owned_tools(old)
                old = MCPConnector(decl=_effective_decl(server, plugin))
                old.catalog_signature = sig
                old.owner = identity()
                old.status = "unknown" if server["enabled"] and binding["enabled"] else "disabled"
            wanted[key] = old
    for key in state.keys() - wanted.keys():
        _drop_owned_tools(state[key])
    _STATES[identity()] = wanted
    return [c for c in wanted.values() if plugin_name is None or c.decl.plugin_name == plugin_name]


def connector_current(conn) -> bool:
    if conn.owner != identity():
        return False
    if not conn.decl.catalog_id:
        return True  # low-level standalone connector (tests / integrations)
    key = f"{conn.decl.catalog_id}:{conn.decl.plugin_name}"
    if _STATES.get(identity(), {}).get(key) is not conn:
        return False
    blob = user_settings.read()
    server = servers(blob).get(conn.decl.catalog_id)
    if not server or not server["enabled"]:
        return False
    binding = bindings(server, blob).get(conn.decl.plugin_name)
    return bool(binding and binding["enabled"] and conn.catalog_signature ==
                _signature(server, conn.decl.plugin_name, binding, blob))


def allowed_host(conn, host: str | None) -> bool:
    patterns = conn.decl.host_patterns
    if "*" in patterns:
        return True
    return host_activation.matches(host, patterns) or host_activation.matches(
        host, host_activation.extra_hosts_map().get(conn.decl.plugin_name, []))


def token_override(decl) -> tuple[bool, str | None]:
    if not decl.catalog_id:
        return False, None
    blob = user_settings.read()
    server = servers(blob).get(decl.catalog_id)
    if not server:
        return True, None
    binding = bindings(server, blob).get(decl.plugin_name, {})
    if binding.get("token"):
        return True, binding["token"]
    if "token" in server:
        return True, server["token"] or None
    return False, None


def validate_url(value: str, *, template: bool = False) -> str:
    value = value.strip()
    if not value:
        return value
    if any(ord(c) < 32 for c in value) or " " in value:
        raise ValueError("MCP URL cannot contain whitespace or control characters")
    if template:
        if value.count("{host}") != 1 or "{" in value.replace("{host}", "") or "}" in value.replace("{host}", ""):
            raise ValueError("URL template must contain exactly one {host} placeholder")
        if not (value.startswith("{host}/") or value.startswith("http://{host}/") or value.startswith("https://{host}/")):
            raise ValueError("URL template must use {host} as its complete host")
        candidate = value.replace("{host}", "example.com")
        if "://" not in candidate:
            candidate = "https://" + candidate
    else:
        if "{" in value or "}" in value:
            raise ValueError("Use url_template for a {host} URL")
        candidate = value
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError:
        raise ValueError("Invalid MCP URL") from None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("MCP URL must be absolute HTTP(S), without credentials or fragment")
    return value


def put_server(sid: str, patch: dict) -> None:
    if not SAFE_ID.fullmatch(sid):
        raise ValueError("Server id must use lowercase letters, digits, underscores or hyphens")
    blob = user_settings.read()
    existing = servers(blob)
    prior = existing.get(sid)
    merged = {**(prior or {}), **patch}
    name = str(merged.get("name", sid)).strip()
    prefix = merged.get("tool_prefix", "mcp_")
    if not name or len(name) > 160:
        raise ValueError("Server name must contain 1–160 characters")
    if not isinstance(prefix, str) or not SAFE_PREFIX.fullmatch(prefix):
        raise ValueError("Tool prefix must start with a letter and contain 1–32 letters, digits or underscores")
    if any(other != sid and entry["tool_prefix"] == prefix for other, entry in existing.items()):
        raise ValueError("Tool prefix is already used by another server")
    url = validate_url(merged.get("url") or "")
    template = validate_url(merged.get("url_template") or "", template=True)
    if not url and not template:
        raise ValueError("An MCP URL or URL template is required")
    conf = blob.setdefault("mcp", {}).setdefault("servers", {}).setdefault(sid, {})
    conf.update({"name": name, "tool_prefix": prefix, "enabled": merged.get("enabled", True)})
    # Preserve legacy URL inheritance until the user actually changes a URL.
    if not prior or "url" in patch or "url_template" in patch:
        conf.update(url=url, url_template=template)
    if "token" in patch:
        conf["token"] = patch["token"].strip()
    user_settings.write(blob)
    list_connectors()


def put_binding(sid: str, plugin: str, patch: dict) -> None:
    from app.plugins import all_plugins
    blob = user_settings.read()
    if sid not in servers(blob):
        raise KeyError("Unknown MCP server")
    if plugin not in {p.name for p in all_plugins()}:
        raise KeyError("Unknown plugin")
    conf = blob.setdefault("plugins", {}).setdefault(plugin, {}).setdefault("mcp_bindings", {}).setdefault(sid, {})
    if "enabled" in patch:
        conf["enabled"] = patch["enabled"]
    if "token" in patch:
        token = patch["token"].strip()
        if token:
            conf["token"] = token
        else:
            conf.pop("token", None)
    user_settings.write(blob)
    list_connectors()


def delete_server(sid: str) -> None:
    blob = user_settings.read()
    server = servers(blob).get(sid)
    if server is None:
        raise KeyError("Unknown MCP server")
    saved = blob.setdefault("mcp", {}).setdefault("servers", {})
    if server["builtin"]:
        saved.setdefault(sid, {})["enabled"] = False
    else:
        saved.pop(sid, None)
        for plugin in _object(blob.get("plugins")).values():
            _object(_object(plugin).get("mcp_bindings")).pop(sid, None)
    user_settings.write(blob)
    list_connectors()


def wire_catalog() -> dict:
    from app.routes.plugins import _connector_to_dict
    from .registry import _dotted_get

    conns = list_connectors()
    blob = user_settings.read()
    out = []
    for server in servers(blob).values():
        entry = {k: v for k, v in server.items() if not k.startswith("_") and k != "token"}
        original = server.get("_original")
        legacy = False
        if original and not server.get("_url_overridden"):
            legacy = bool((original.token_setting and _dotted_get(blob, original.token_setting)) or
                          (original.token_map_setting and _dotted_get(blob, original.token_map_setting)))
        entry["has_token"] = bool(server.get("token"))
        entry["has_legacy_token"] = legacy
        entry["bindings"] = [{"plugin": plugin, "enabled": bool(binding["enabled"]),
                              "has_token": bool(binding.get("token"))}
                             for plugin, binding in bindings(server, blob).items()]
        entry["connectors"] = [{**_connector_to_dict(c), "plugin": c.decl.plugin_name}
                               for c in conns if c.decl.catalog_id == server["id"]]
        out.append(entry)
    return {"servers": out}


def redact_plugins(raw: Any) -> dict:
    """Deep-copy plugin settings and redact new and manifest-declared secrets."""
    import copy
    from .registry import MCP_CONNECTORS
    result = copy.deepcopy(_object(raw))
    for conf in result.values():
        for binding in _object(_object(conf).get("mcp_bindings")).values():
            if isinstance(binding, dict):
                binding["has_token"] = bool(binding.pop("token", None))
    # Existing manifest credentials were historically exposed by /api/settings.
    # Keep paths/configuration but replace bearer values with presence flags.
    for conn in MCP_CONNECTORS.values():
        for path in (conn.decl.token_setting, conn.decl.token_map_setting):
            if not path or not path.startswith("plugins."):
                continue
            parts = path.split(".")[1:]
            parent = result
            for part in parts[:-1]:
                parent = _object(parent.get(part))
            if parts[-1] in parent:
                value = parent.pop(parts[-1])
                parent["has_" + parts[-1]] = (
                    {host: bool(token) for host, token in value.items()}
                    if path == conn.decl.token_map_setting and isinstance(value, dict)
                    else bool(value)
                )
    return result
