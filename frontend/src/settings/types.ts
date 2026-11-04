// Shared types between the SettingsView, plugin panels, and the
// /api/plugins fetcher.

export interface PluginSchemaField {
  key: string;
  // "secret_per_host" renders one secret input per activation host
  // (built-in patterns + user extras) and stores a {host: value} map
  // at `key` — saved as ONE dotted patch (host strings contain dots,
  // so per-entry dotted paths would split wrongly).
  type: "text" | "url" | "secret" | "secret_per_host" | "bool" | "enum";
  label: string;
  default?: unknown;
  help?: string;
  placeholder?: string;
  options?: { value: string; label: string }[];
}

export interface PluginSchema {
  title?: string;
  fields: PluginSchemaField[];
  // ``mcp:<connector_id>`` — pulls the connector with this id from the
  // plugin's ``mcp_connectors`` list and renders a status badge below
  // the form.
  status_probe?: string;
}

export interface ConnectorEndpointStatus {
  url: string;
  status: "ok" | "unauth" | "unreachable";
  last_error: string | null;
  tool_count: number;
  tool_names: string[];
}

export interface PluginConnectorStatus {
  id: string;
  // Aggregate: "ok" when at least one endpoint is reachable.
  status: "ok" | "unauth" | "unreachable" | "not_configured" | "unknown" | "disabled";
  last_error: string | null;
  // Endpoint the last successful refresh used. For url_template
  // connectors ("{host}/mcp") tool calls re-derive the URL from the
  // page host; this is the probe/fallback endpoint.
  active_url?: string | null;
  url_template?: string | null;
  // Fixed vendor endpoint baked into the manifest (no {host} to fill,
  // nothing for the user to configure) — e.g. Runpod's hosted server.
  url?: string | null;
  // Per-endpoint probe results (one per activation host for template
  // connectors) from the last refresh.
  endpoints?: ConnectorEndpointStatus[];
  tool_count: number;
  tool_names: string[];
}

export interface PluginInfo {
  name: string;
  rel_dir?: string;  // path relative to plugins/ root, e.g. "google/drive"
  version?: string | null;
  description?: string | null;
  agent_name?: string | null;
  host_patterns: string[];
  // User-added activation hosts (Settings → Plugins tab), persisted at
  // ``plugins.<name>.extra_hosts``. May carry a port ("127.0.0.1:8756").
  extra_hosts?: string[];
  settings_schema: PluginSchema | null;
  settings_panel: "schema" | "custom";
  mcp_connectors: PluginConnectorStatus[];
}

export interface CustomPanelProps {
  pluginName: string;
  backendOrigin: string;
}
