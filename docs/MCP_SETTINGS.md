# Global MCP settings

Settings → **MCP** manages a shared catalog of remote MCP servers for the current user. Inspired by Voitta Desktop's server cards, each entry has a display name, tool prefix, endpoint, enabled switch, and optional default bearer token. The bookmarklet currently supports **Streamable HTTP**; this editor does not launch desktop subprocesses or stdio servers.

## Configure and connect

1. Add a server in the MCP tab. Use an absolute HTTP(S) URL without embedded credentials and a unique tool prefix (1–32 letters/digits/underscores, beginning with a letter).
2. Save the server. A blank token input preserves the saved token; the explicit clear checkbox removes it.
3. Under **Plugin access & token overrides**, enable the plugins that should use the server. The same controls appear in each expandable card under Settings → Plugins, including plugins with no original MCP declaration.
4. In the plugin’s **Authentication** selector, keep **Use this server’s default token**, or choose **Override token for this plugin** and enter its token. Blank override inputs preserve existing overrides. Switching back to the server default and saving removes the plugin override.
5. Click **Connect / refresh** to discover the remote tools. Configuration saves do not imply a successful connection; the status and tool count come from the probe.

Servers can be shared across plugins without copying endpoints. Tools are still limited to the plugins' activation hosts. Default bearer tokens belong to individual servers—not to every arbitrary MCP endpoint. Server definitions, bindings, tokens, and runtime connection state are user-scoped in server mode.

## Existing plugins

Manifest `mcp_servers` declarations seed built-in catalog entries. The original plugin binding retains its original tool names. Additional bindings use plugin-scoped names to avoid collisions. Built-in servers can be disabled; custom servers can be removed with confirmation.

Page-host URL templates such as `{host}/mcp` remain supported for existing connectors. A fixed URL takes precedence over a template. When sharing such a connector with another plugin, configure a fixed URL if both plugins should call the same server rather than derive an endpoint from each page's host.

Existing per-host keys are retained for compatibility. Credential precedence is:

1. Plugin binding token override.
2. Explicit server default token (an explicitly cleared default means no token).
3. Original binding's legacy per-host key.
4. Original binding's legacy fallback key.

New bindings do not inherit another plugin's legacy credentials. Configure a server default or a binding override for them.

## Secret handling

The catalog API returns `has_token` flags, never bearer-token values. Legacy token maps similarly expose per-host presence flags. Password inputs do not prefill stored secrets; explicit Save/Clear actions prevent an unrelated edit from replacing a stored key. Per-host edits merge only the selected host.

Storage uses the existing backend user-settings file and its restrictive file permissions; this is not encrypted credential storage. The new UI does not read live desktop configuration or import desktop credentials.

## API

- `GET /api/mcp`: server catalog, bindings and connector status.
- `PUT /api/mcp/servers/{id}`: create/update a server; omitted `token` preserves, `""` clears.
- `DELETE /api/mcp/servers/{id}`: remove a custom server or disable a built-in entry.
- `PUT /api/mcp/servers/{id}/plugins/{plugin}`: enable/disable a binding and optionally update its token; `""` removes the override.
- `POST /api/mcp/servers/{id}/refresh`: probe enabled bindings and refresh their tools.

Mutation responses use the same catalog shape as GET. Refresh existing bookmarklet widgets after rebuilding the frontend; restart the backend to load new routes and runtime changes.
