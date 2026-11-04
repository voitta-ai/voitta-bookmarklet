// Settings → Plugins tab: per-plugin activation hosts + settings.
//
// One card per loaded plugin showing where it activates: the manifest
// ``host_patterns`` (read-only — baked into the plugin) plus the
// user's own extra hosts (editable chips). Extra hosts persist at
// ``plugins.<name>.extra_hosts`` via the dotted settings patch and the
// backend matches them live, so a newly added host takes effect on the
// next chat turn — no restart.
//
// Entries may carry a port ("127.0.0.1:8756"); the backend then
// requires the page's port to match, so a plugin pinned to one local
// app doesn't light up on every other localhost port.
//
// Every card expands to show global MCP server bindings and token
// overrides. Plugins with a declarative settings_schema also show their
// existing settings and legacy per-host credential controls.

import { useState } from "react";
import { saveSettings } from "../lib/settings";
import PluginSettingsPanel from "./PluginSettingsPanel";
import { PluginMCPSettings } from "./MCPSettingsPanel";
import type { PluginInfo } from "./types";

interface Props {
  plugins: PluginInfo[];
  backendOrigin: string;
  // Refetch /api/plugins after a save so extra_hosts stays in sync.
  onSaved: () => void;
  // "Refresh tool list" for a plugin's MCP connectors (shared with the
  // tab view — SettingsView owns the fetch + busy state). With a host,
  // only that activation host's endpoint is re-probed; busy keys are
  // "name" or "name@host".
  onRefreshPlugin: (name: string, host?: string) => void;
  refreshBusy: Record<string, boolean>;
}

/** "https://x.y:8756/path", "x.y:8756/", "X.Y." → "x.y:8756" (host[:port]). */
function normalizeHost(raw: string): string | null {
  let s = raw.trim().toLowerCase();
  if (!s) return null;
  s = s.replace(/^[a-z][a-z0-9+.-]*:\/\//, "").replace(/^\/\//, "");
  s = s.split("/")[0].split("?")[0].split("#")[0];
  const at = s.lastIndexOf("@");
  if (at !== -1) s = s.slice(at + 1);
  s = s.replace(/\.+$/, "").replace(/:$/, "");
  return s || null;
}

export default function PluginActivationPanel({
  plugins,
  backendOrigin,
  onSaved,
  onRefreshPlugin,
  refreshBusy,
}: Props) {
  if (plugins.length === 0) {
    return <p className="muted">No plugins loaded.</p>;
  }
  return (
    <div className="plugin-activation-panel">
      <h3 style={{ margin: "0 0 4px", fontSize: 14 }}>Plugin activation</h3>
      <p className="muted" style={{ margin: "0 0 12px", fontSize: 12 }}>
        Controls where each plugin's tools and branding turn on. Built-in
        hosts come from the plugin manifest; you can add your own (with an
        optional <code>:port</code>). Changes apply on the next message.
        Expand a plugin to select MCP servers, override bearer tokens,
        and edit its other settings.
      </p>
      {plugins.map((p) => (
        <PluginCard
          key={p.name}
          plugin={p}
          backendOrigin={backendOrigin}
          onSaved={onSaved}
          onRefresh={(host?: string) => onRefreshPlugin(p.name, host)}
          refreshBusy={refreshBusy}
        />
      ))}
    </div>
  );
}

function PluginCard({
  plugin,
  backendOrigin,
  onSaved,
  onRefresh,
  refreshBusy,
}: {
  plugin: PluginInfo;
  backendOrigin: string;
  onSaved: () => void;
  onRefresh: (host?: string) => void;
  refreshBusy: Record<string, boolean>;
}) {
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [open, setOpen] = useState(false);

  const extras = plugin.extra_hosts ?? [];
  const everywhere = plugin.host_patterns.includes("*");
  const expandable = true; // Every plugin can bind a global MCP server.

  async function persist(next: string[]) {
    setBusy(true);
    setErr(null);
    try {
      await saveSettings(backendOrigin, {
        // Empty list → null so the key is deleted server-side.
        dotted: { [`plugins.${plugin.name}.extra_hosts`]: next.length ? next : null },
      });
      onSaved();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }

  function add(raw: string) {
    const host = normalizeHost(raw);
    setDraft("");
    if (!host) return;
    if (extras.includes(host) || plugin.host_patterns.includes(host)) return;
    void persist([...extras, host]);
  }

  function remove(host: string) {
    void persist(extras.filter((h) => h !== host));
  }

  const mcpSummary = plugin.mcp_connectors
    .map((c) => `${c.id} · ${c.status}${c.status === "ok" ? ` · ${c.tool_count} tools` : ""}`)
    .join(", ");

  return (
    <div
      style={{
        marginBottom: 10,
        padding: "10px 12px",
        border: "1px solid var(--voitta-border, #d1d5db)",
        borderRadius: 6,
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "baseline",
          gap: 8,
          cursor: expandable ? "pointer" : undefined,
        }}
        role={expandable ? "button" : undefined}
        aria-expanded={expandable ? open : undefined}
        onClick={expandable ? () => setOpen((o) => !o) : undefined}
      >
        {expandable && (
          <span aria-hidden style={{ fontSize: 11, width: 12, flexShrink: 0 }}>
            {open ? "▾" : "▸"}
          </span>
        )}
        <strong style={{ fontSize: 13 }}>{plugin.agent_name || plugin.name}</strong>
        {plugin.agent_name && plugin.agent_name !== plugin.name && (
          <code className="muted" style={{ fontSize: 11 }}>{plugin.name}</code>
        )}
        {mcpSummary && (
          <span className="muted" style={{ marginLeft: "auto", fontSize: 11 }}>
            MCP: {mcpSummary}
          </span>
        )}
      </div>
      {plugin.description && (
        <p className="muted" style={{ margin: "2px 0 0", fontSize: 11 }}>
          {plugin.description}
        </p>
      )}

      <div style={{ marginTop: 8, fontSize: 12 }}>
        <HostRow label="Built-in">
          {everywhere ? (
            <span className="muted">active everywhere (*)</span>
          ) : (
            plugin.host_patterns.map((h) => <Chip key={h} text={h} />)
          )}
        </HostRow>
        {!everywhere && (
          <HostRow label="Yours">
            {extras.length === 0 && <span className="muted">(none)</span>}
            {extras.map((h) => (
              <Chip key={h} text={h} onRemove={busy ? undefined : () => remove(h)} />
            ))}
          </HostRow>
        )}
      </div>

      {!everywhere && (
        <div style={{ marginTop: 8, display: "flex", gap: 8, alignItems: "center" }}>
          <input
            type="text"
            spellCheck={false}
            autoComplete="off"
            placeholder="http://host:port or domain…"
            value={draft}
            disabled={busy}
            onChange={(e) => setDraft(e.currentTarget.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") add(draft);
            }}
            style={{ flex: 1, fontSize: 12, padding: "4px 8px" }}
          />
          <button
            type="button"
            className="save-btn"
            disabled={busy || !draft.trim()}
            onClick={() => add(draft)}
            style={{ padding: "4px 10px", fontSize: 12 }}
          >
            Add
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => add(location.host)}
            title={`Add ${location.host}`}
            style={{
              padding: "4px 10px",
              fontSize: 12,
              background: "transparent",
              border: "1px solid var(--voitta-border, #d1d5db)",
              borderRadius: 5,
              cursor: "pointer",
              color: "var(--voitta-text)",
            }}
          >
            Use current tab
          </button>
        </div>
      )}
      {err && (
        <p className="muted" style={{ margin: "6px 0 0", color: "#b00020", fontSize: 11 }}>
          Save failed: {err}
        </p>
      )}

      {open && (
        <PluginMCPSettings backendOrigin={backendOrigin} pluginName={plugin.name} onSaved={onSaved} />
      )}
      {expandable && open && plugin.settings_schema && (
        <div
          style={{
            marginTop: 10,
            paddingTop: 10,
            borderTop: "1px solid var(--voitta-border, #d1d5db)",
          }}
        >
          <PluginSettingsPanel
            pluginName={plugin.name}
            schema={plugin.settings_schema}
            connectors={plugin.mcp_connectors}
            backendOrigin={backendOrigin}
            onRefreshConnectors={() => onRefresh()}
            refreshBusy={!!refreshBusy[plugin.name]}
            hosts={[
              ...plugin.host_patterns.filter((h) => !h.includes("*")),
              ...extras,
            ]}
            onRefreshHost={(host) => onRefresh(host)}
            hostBusy={Object.fromEntries(
              Object.entries(refreshBusy)
                .filter(([k]) => k.startsWith(`${plugin.name}@`))
                .map(([k, v]) => [k.slice(plugin.name.length + 1), v]),
            )}
          />
        </div>
      )}
    </div>
  );
}

function HostRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 4, flexWrap: "wrap" }}>
      <span className="muted" style={{ width: 56, flexShrink: 0, fontSize: 11 }}>{label}</span>
      {children}
    </div>
  );
}

function Chip({ text, onRemove }: { text: string; onRemove?: () => void }) {
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 4,
        padding: "1px 8px",
        border: "1px solid var(--voitta-border, #d1d5db)",
        borderRadius: 999,
        background: "var(--voitta-accent-tint, #f9fafb)",
        fontSize: 11,
        fontFamily: "monospace",
      }}
    >
      {text}
      {onRemove && (
        <button
          type="button"
          aria-label={`Remove ${text}`}
          onClick={onRemove}
          style={{
            border: "none",
            background: "transparent",
            cursor: "pointer",
            padding: 0,
            fontSize: 11,
            lineHeight: 1,
            color: "var(--voitta-text)",
          }}
        >
          ✕
        </button>
      )}
    </span>
  );
}
