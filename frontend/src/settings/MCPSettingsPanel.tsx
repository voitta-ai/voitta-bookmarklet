import { useCallback, useEffect, useState } from "react";
import { mcpRequest, serverPath, tokenPatch, type MCPServer } from "../lib/mcp";
import type { PluginInfo } from "./types";

function serverLabel(server: MCPServer): string {
  // Friendly labels for manifest defaults; user-chosen names are untouched.
  if (server.name === "voitta-enterprise / vre") return "Voitta RAG";
  return server.name;
}
const rowStyle = { display: "flex", gap: 8, flexWrap: "wrap" as const, alignItems: "center" };
const fieldStyle = { display: "grid", gap: 4, marginBottom: 10, fontSize: 12 };

function useCatalog(backendOrigin: string) {
  const [servers, setServers] = useState<MCPServer[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const reload = useCallback(async () => {
    try {
      const body = await mcpRequest(backendOrigin);
      setServers(body.servers);
      setError("");
    } catch (err) {
      setError(String(err));
    } finally {
      setLoading(false);
    }
  }, [backendOrigin]);
  useEffect(() => { void reload(); }, [reload]);
  return { servers, error, loading, reload };
}

export default function MCPSettingsPanel({ backendOrigin, plugins, onSaved }: {
  backendOrigin: string;
  plugins: PluginInfo[];
  onSaved: () => void;
}) {
  const { servers, error, loading, reload } = useCatalog(backendOrigin);
  const [newServer, setNewServer] = useState<MCPServer | null>(null);
  async function saved() {
    await reload();
    onSaved();
  }
  return (
    <div className="mcp-settings">
      <div className="mcp-heading">
        <h3>MCP servers</h3>
        <p>One server, shared across your plugins.</p>
      </div>
      <p className="mcp-intro">Give each server a URL and its own default bearer token.
        Plugins use that token unless you set an override for that plugin.</p>
      {loading && <p className="muted">Loading MCP servers…</p>}
      {error && <p role="alert">{error} <button onClick={() => void reload()}>Retry</button></p>}
      {servers.map((server) => (
        <ServerCard key={server.id} server={server} backendOrigin={backendOrigin}
          plugins={plugins} onSaved={saved} />
      ))}
      {newServer ? (
        <ServerCard server={newServer} backendOrigin={backendOrigin} plugins={plugins}
          isNew onCancel={() => setNewServer(null)} onSaved={async () => {
            setNewServer(null);
            await saved();
          }} />
      ) : (
        <button type="button" onClick={() => setNewServer({
          id: `server-${crypto.randomUUID()}`, name: "", url: "", url_template: null,
          tool_prefix: "", transport: "streamable-http", enabled: true,
          builtin: false, has_token: false, bindings: [], connectors: [],
        })}>Add MCP server</button>
      )}
    </div>
  );
}

function ServerCard({ server, backendOrigin, plugins, onSaved, isNew = false, onCancel }: {
  server: MCPServer;
  backendOrigin: string;
  plugins: PluginInfo[];
  onSaved: () => Promise<void>;
  isNew?: boolean;
  onCancel?: () => void;
}) {
  const [name, setName] = useState(serverLabel(server));
  const [url, setUrl] = useState(server.url || "");
  const [template, setTemplate] = useState(server.url_template || "");
  const [prefix, setPrefix] = useState(server.tool_prefix);
  const [enabled, setEnabled] = useState(server.enabled);
  const [token, setToken] = useState("");
  const [clear, setClear] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);
  useEffect(() => {
    setName(serverLabel(server)); setUrl(server.url || "");
    setTemplate(server.url_template || ""); setPrefix(server.tool_prefix);
    setEnabled(server.enabled);
  }, [server.name, server.url, server.url_template, server.tool_prefix, server.enabled]);

  async function perform(action: () => Promise<unknown>, success: string, savedConfig = false) {
    setBusy(true); setError(""); setMessage("");
    try {
      await action();
      if (savedConfig) { setToken(""); setClear(false); }
      setConfirmDelete(false);
      await onSaved();
      setMessage(success);
    } catch (err) { setError(String(err)); }
    finally { setBusy(false); }
  }

  async function save() {
    setError("");
    if (!name.trim() || !/^[A-Za-z][A-Za-z0-9_]{0,31}$/.test(prefix.trim())) {
      setError("Enter a name and a unique tool prefix: 1–32 letters, digits or underscores, starting with a letter.");
      return;
    }
    if (!url.trim() && !template.trim()) {
      setError("Enter a server URL or a page-host URL template."); return;
    }
    if (url.trim()) {
      try {
        const parsed = new URL(url.trim());
        if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password) throw new Error();
      } catch { setError("Use an absolute HTTP(S) URL without embedded credentials."); return; }
    }
    if (template && !template.includes("{host}")) {
      setError("The URL template must contain {host}."); return;
    }
    await perform(() => mcpRequest(backendOrigin, serverPath(server.id), "PUT", {
      name: name.trim(), url: url.trim() || null, url_template: template.trim() || null,
      tool_prefix: prefix.trim(), enabled, ...tokenPatch(token, clear),
    }), "Saved. Connect / refresh to discover tools.", true);
  }

  return (
    <section className="mcp-card" aria-label={isNew ? "New MCP server" : serverLabel(server)}>
      <div className="mcp-card-heading">
        <div><strong>{isNew ? "New MCP server" : serverLabel(server)}</strong>
          <p className="mcp-endpoint">{server.url || (server.url_template ? "Legacy page-host endpoint · set a fixed URL to share this server" : "Configure your connection")}</p>
        </div>
        <span className="mcp-badge">HTTP</span>
      </div>
      <fieldset disabled={busy} style={{ border: 0, padding: 0, margin: 0, minWidth: 0 }}>
        <label style={fieldStyle}>Display name
          <input type="text" value={name} onChange={(e) => setName(e.target.value)} autoComplete="off" />
        </label>
        <label style={fieldStyle}>Server URL
          <input type="url" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://server.example/mcp" autoComplete="off" spellCheck={false} />
        </label>
        <div className="mcp-auth-section">
        <label style={fieldStyle}>Default bearer token
          <input type="text" className="secret" value={token} disabled={clear}
            placeholder={server.has_token ? "Token saved — leave blank to keep" : "Optional — no token saved"}
            onChange={(e) => setToken(e.target.value)} autoComplete="off" autoCorrect="off" autoCapitalize="off" spellCheck={false} />
        </label>
        <p className="mcp-help">Used by every plugin connected to <strong>{name || "this server"}</strong>, unless that plugin has its own override.</p>
        {server.has_token && <label className="mcp-check"><input type="checkbox" checked={clear} onChange={(e) => setClear(e.target.checked)} /> Clear saved default token</label>}
        {server.has_legacy_token && <p className="mcp-note">The original plugin still has a legacy key. To share this connection, set a fixed server URL and a default token here. No keys are moved automatically.</p>}
        </div>
        <details className="mcp-advanced" open={isNew || undefined}>
          <summary>Advanced connection settings</summary>
          <label style={fieldStyle}>Tool prefix
            <input type="text" value={prefix} onChange={(e) => setPrefix(e.target.value)} placeholder="research_" spellCheck={false} />
          </label>
          {(server.url_template || template) && <label style={fieldStyle}>Page-host URL template (used only without a fixed URL)
            <input type="text" value={template} onChange={(e) => setTemplate(e.target.value)} spellCheck={false} />
          </label>}
        </details>
        <label className="mcp-check mcp-enabled"><input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} /> Server enabled</label>
        <div style={rowStyle}>
          <button type="button" className="save-btn" onClick={() => void save()}>{busy ? "Working…" : "Save server"}</button>
          {isNew ? <button type="button" onClick={onCancel}>Cancel</button> : <>
            <button type="button" disabled={!server.enabled || !server.bindings.some((binding) => binding.enabled)}
              onClick={() => void perform(() => mcpRequest(backendOrigin, `${serverPath(server.id)}/refresh`, "POST"), "Connection check complete; see status below.")}>Connect / refresh</button>
            {!server.builtin && (confirmDelete ? <>
              <button type="button" onClick={() => void perform(() => mcpRequest(backendOrigin, serverPath(server.id), "DELETE"), "Server removed.")}>Confirm remove</button>
              <button type="button" onClick={() => setConfirmDelete(false)}>Cancel remove</button>
            </> : <button type="button" onClick={() => setConfirmDelete(true)}>Remove</button>)}
          </>}
        </div>
      </fieldset>
      {error && <p role="alert">{error}</p>}
      {message && <p role="status" className="muted">{message}</p>}
      {!isNew && <>
        {!server.bindings.some((binding) => binding.enabled) && <p className="muted" style={{ fontSize: 12 }}>Enable at least one plugin below before connecting.</p>}
        {server.connectors.map((conn) => (
          <p key={`${conn.plugin}:${conn.id}`} className="muted" style={{ fontSize: 12 }}>
            {conn.plugin}: {conn.status} · {conn.tool_count} tools
          </p>
        ))}
        <details className="mcp-plugin-list">
          <summary>Used by plugins <span className="mcp-badge">{server.bindings.filter((binding) => binding.enabled).length} enabled</span></summary>
          <p className="mcp-help">All enabled plugins share this server’s URL and default token. Override a token only when a plugin needs different credentials.</p>
          {plugins.map((plugin) => (
            <BindingRow key={plugin.name} server={server} pluginName={plugin.name}
              label={plugin.agent_name || plugin.name} backendOrigin={backendOrigin} onSaved={onSaved} />
          ))}
        </details>
      </>}
    </section>
  );
}

export function PluginMCPSettings({ backendOrigin, pluginName, onSaved }: {
  backendOrigin: string;
  pluginName: string;
  onSaved: () => void;
}) {
  const { servers, error, loading, reload } = useCatalog(backendOrigin);
  return (
    <div className="mcp-settings mcp-plugin-settings">
      <h4 style={{ margin: "0 0 6px" }}>MCP servers</h4>
      <p className="muted" style={{ fontSize: 12 }}>Server URLs and default tokens are managed in the MCP tab. Enable a server here and optionally override its token for this plugin.</p>
      {loading && <p className="muted">Loading…</p>}
      {error && <p role="alert">{error} <button onClick={() => void reload()}>Retry</button></p>}
      {!loading && !error && servers.length === 0 && <p className="muted">Add a server in the MCP tab first.</p>}
      {servers.map((server) => (
        <BindingRow key={server.id} server={server} pluginName={pluginName}
          label={serverLabel(server)} backendOrigin={backendOrigin} onSaved={async () => {
            await reload(); onSaved();
          }} />
      ))}
    </div>
  );
}

function BindingRow({ server, pluginName, label, backendOrigin, onSaved }: {
  server: MCPServer;
  pluginName: string;
  label: string;
  backendOrigin: string;
  onSaved: () => Promise<void>;
}) {
  const binding = server.bindings.find((b) => b.plugin === pluginName);
  const [enabled, setEnabled] = useState(binding?.enabled ?? false);
  const [token, setToken] = useState("");
  const [override, setOverride] = useState(binding?.has_token ?? false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  useEffect(() => { setEnabled(binding?.enabled ?? false); }, [binding?.enabled]);
  useEffect(() => { setOverride(binding?.has_token ?? false); }, [binding?.has_token]);
  async function save() {
    if (override && !binding?.has_token && !token.trim()) {
      setError("Enter an override token, or choose the server default."); return;
    }
    setBusy(true); setError(""); setSaved(false);
    try {
      await mcpRequest(backendOrigin, `${serverPath(server.id)}/plugins/${encodeURIComponent(pluginName)}`, "PUT", {
        enabled, ...tokenPatch(override ? token : "", !override && !!binding?.has_token),
      });
      setToken("");
      await onSaved(); setSaved(true);
    } catch (err) { setError(String(err)); }
    finally { setBusy(false); }
  }
  return (
    <fieldset disabled={busy} className="mcp-binding">
      <legend>{label}</legend>
      <label className="mcp-check">
        <input type="checkbox" checked={enabled} onChange={(e) => { setEnabled(e.target.checked); setSaved(false); }} />
        Enable for this plugin {!server.enabled && <span className="muted">(server disabled globally)</span>}
      </label>
      <label className="mcp-token-mode">Authentication
        <select value={override ? "override" : "default"} onChange={(e) => { setOverride(e.target.value === "override"); setSaved(false); }}>
          <option value="default">Use this server’s default token</option>
          <option value="override">Override token for this plugin</option>
        </select>
      </label>
      {override ? <label style={fieldStyle}>Bearer token override
        <input type="text" className="secret" value={token}
          placeholder={binding?.has_token ? "Override saved — leave blank to keep" : "Enter this plugin’s token"}
          autoComplete="off" autoCorrect="off" autoCapitalize="off" spellCheck={false}
          onChange={(e) => { setToken(e.target.value); setSaved(false); }} />
      </label> : <p className="mcp-help">
        {server.has_token ? `Inherits the token saved for ${serverLabel(server)}.`
          : server.has_legacy_token ? "No shared default saved yet. The original plugin may still use its legacy key."
          : "No token is saved for this server. Add one in the MCP tab if authentication is required."}
        {binding?.has_token && " Saving will remove the existing plugin override."}
      </p>}
      <button type="button" onClick={() => void save()}>{busy ? "Saving…" : "Save plugin access"}</button>
      {saved && <span role="status" className="muted" style={{ marginLeft: 8 }}>Saved — refresh the server to discover tools.</span>}
      {error && <p role="alert">{error}</p>}
    </fieldset>
  );
}
