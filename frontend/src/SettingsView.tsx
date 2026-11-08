/// <reference types="vite/client" />
// Tabbed settings pane.
//
// Tabs:
//   • "Global" → core fields (provider, key, model, layout, theme)
//   • "<plugin>" → one per loaded plugin that declares either a
//                  ``settings_schema`` (declarative) or a custom
//                  ``frontend/settings-panel.tsx`` (React component).
//
// Plugin tab discovery is driven by GET /api/plugins. Custom panels are
// auto-discovered via ``import.meta.glob`` of
// ``plugins/<name>/frontend/settings-panel.tsx`` — same mechanism the
// widget uses for plugin primitives.

import { useCallback, useEffect, useState } from "react";
import GlobalSettings from "./settings/GlobalSettings";
import PluginActivationPanel from "./settings/PluginActivationPanel";
import PluginSettingsPanel from "./settings/PluginSettingsPanel";
import RoutinesPanel from "./settings/RoutinesPanel";
import MCPSettingsPanel from "./settings/MCPSettingsPanel";
import AccountPanel from "./settings/AccountPanel";
import UsersPanel from "./settings/UsersPanel";
import { useAuth } from "./lib/auth";
import type {
  CustomPanelProps,
  PluginConnectorStatus,
  PluginInfo,
} from "./settings/types";

interface Props {
  backendOrigin: string;
  onClose: () => void;
  // Open on a given tab (e.g. the header alarm clock → Routines).
  initialTab?: string;
  routineFocus?: { id: string | null; createNew: boolean };
}

export const ROUTINES_TAB = "__routines__";
export const USERS_TAB = "__users__";
export const ACCOUNT_TAB = "__account__";

// Eager-globbed custom settings panels. Each plugin's
// ``frontend/settings-panel.tsx`` exports a default React component
// matching ``CustomPanelProps``. The glob walks up two dirs from this
// file (src/) so the relative path is ``../../plugins/...``.
type CustomPanelModule = {
  default: (props: CustomPanelProps) => JSX.Element;
};
const CUSTOM_PANEL_MODULES = import.meta.glob<CustomPanelModule>(
  "../../plugins/**/frontend/settings-panel.tsx",
  { eager: true },
);

// Tab id for the fixed "Plugins" (activation) tab. Namespaced so a
// plugin literally named "plugins" can't shadow it.
const ACTIVATION_TAB = "__activation__";
const MCP_TAB = "__mcp__";

// relDir is the plugin's path relative to plugins/ root (e.g. "google/drive").
// Falls back to bare plugin name for flat plugins without a rel_dir from the API.
function findCustomPanel(relDir: string) {
  const needle = `/plugins/${relDir}/frontend/settings-panel.tsx`;
  for (const path in CUSTOM_PANEL_MODULES) {
    if (path.endsWith(needle)) {
      return CUSTOM_PANEL_MODULES[path].default;
    }
  }
  return null;
}

export default function SettingsView({ backendOrigin, onClose, initialTab, routineFocus }: Props) {
  const [plugins, setPlugins] = useState<PluginInfo[]>([]);
  const [activeTab, setActiveTab] = useState<string>(initialTab ?? "global");
  const [refreshBusy, setRefreshBusy] = useState<Record<string, boolean>>({});
  const [loadErr, setLoadErr] = useState<string | null>(null);
  const auth = useAuth();
  const showUsers = auth.enabled && auth.isAdmin;

  const fetchPlugins = useCallback(() => {
    fetch(`${backendOrigin}/api/plugins`, { credentials: "include" })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`status ${r.status}`))))
      .then((body: { plugins: PluginInfo[] }) => {
        if (body && Array.isArray(body.plugins)) {
          setPlugins(body.plugins);
          setLoadErr(null);
        }
      })
      .catch((err) => {
        // Settings still works — Global tab doesn't depend on this.
        setLoadErr(String(err));
        console.warn("[voitta] /api/plugins failed:", err);
      });
  }, [backendOrigin]);

  useEffect(() => {
    fetchPlugins();
  }, [fetchPlugins]);

  // host given → re-probe just that activation host's endpoint (the
  // per-row Connect button); omitted → probe everything. Busy keys are
  // "name" or "name@host" so rows spin independently.
  async function onRefreshPlugin(name: string, host?: string) {
    const busyKey = host ? `${name}@${host}` : name;
    setRefreshBusy((m) => ({ ...m, [busyKey]: true }));
    try {
      const qs = host ? `?host=${encodeURIComponent(host)}` : "";
      const r = await fetch(
        `${backendOrigin}/api/plugins/${encodeURIComponent(name)}/refresh${qs}`,
        { method: "POST", credentials: "include" },
      );
      if (!r.ok) throw new Error(`status ${r.status}`);
      const body = (await r.json()) as {
        plugin: string;
        connectors: PluginConnectorStatus[];
      };
      setPlugins((ps) =>
        ps.map((p) =>
          p.name === body.plugin ? { ...p, mcp_connectors: body.connectors } : p,
        ),
      );
    } catch (err) {
      console.warn("[voitta] refresh failed:", err);
    } finally {
      setRefreshBusy((m) => {
        const copy = { ...m };
        delete copy[busyKey];
        return copy;
      });
    }
  }

  // Plugin gets its own tab only when it ships a custom settings panel
  // (Google's OAuth dance). Schema-declared settings render inside the
  // plugin's expandable card on the Plugins tab instead.
  const tabbedPlugins = plugins.filter((p) =>
    findCustomPanel(p.rel_dir ?? p.name),
  );

  return (
    <section className="view view-settings">
      <TabStrip
        plugins={tabbedPlugins}
        active={activeTab}
        onChange={setActiveTab}
        showUsers={showUsers}
      />

      <div className="settings-tab-body" style={{ marginTop: 12 }}>
        {activeTab === "global" && <GlobalSettings backendOrigin={backendOrigin} />}
        {activeTab === ACTIVATION_TAB && (
          <PluginActivationPanel
            plugins={plugins}
            backendOrigin={backendOrigin}
            onSaved={fetchPlugins}
            onRefreshPlugin={onRefreshPlugin}
            refreshBusy={refreshBusy}
          />
        )}
        {activeTab === MCP_TAB && (
          <MCPSettingsPanel backendOrigin={backendOrigin} plugins={plugins} onSaved={fetchPlugins} />
        )}
        {activeTab === ROUTINES_TAB && (
          <RoutinesPanel
            backendOrigin={backendOrigin}
            focusId={routineFocus?.id}
            createNew={routineFocus?.createNew}
          />
        )}
        {activeTab === USERS_TAB && showUsers && <UsersPanel backendOrigin={backendOrigin} />}
        {activeTab === ACCOUNT_TAB && <AccountPanel backendOrigin={backendOrigin} />}
        {![ "global", ACTIVATION_TAB, ROUTINES_TAB, MCP_TAB, USERS_TAB, ACCOUNT_TAB ].includes(activeTab) && (
          <PluginTabBody
            pluginName={activeTab}
            plugin={tabbedPlugins.find((p) => p.name === activeTab)}
            backendOrigin={backendOrigin}
            onRefresh={() => onRefreshPlugin(activeTab)}
            refreshBusy={!!refreshBusy[activeTab]}
          />
        )}
      </div>

      <LoadedPluginsFooter plugins={plugins} loadErr={loadErr} />

      <div className="actions" style={{ marginTop: 12 }}>
        <button
          type="button"
          onClick={onClose}
          style={{
            background: "transparent",
            border: "1px solid var(--voitta-border)",
            padding: "8px 12px",
            borderRadius: 5,
            cursor: "pointer",
            color: "var(--voitta-text)",
          }}
        >
          Back
        </button>
      </div>
    </section>
  );
}

function TabStrip({
  plugins,
  active,
  onChange,
  showUsers,
}: {
  plugins: PluginInfo[];
  active: string;
  onChange: (name: string) => void;
  showUsers: boolean;
}) {
  const tabs: { id: string; label: string }[] = [
    { id: "global", label: "Global" },
    { id: ACTIVATION_TAB, label: "Plugins" },
    { id: MCP_TAB, label: "MCP" },
    { id: ROUTINES_TAB, label: "Routines" },
    ...(showUsers ? [{ id: USERS_TAB, label: "Users" }] : []),
    { id: ACCOUNT_TAB, label: "Account" },
    ...plugins.map((p) => ({ id: p.name, label: p.agent_name || p.name })),
  ];
  return (
    <div
      className="settings-tabs"
      role="tablist"
      style={{
        display: "flex",
        flexWrap: "wrap",
        gap: 4,
        borderBottom: "1px solid var(--voitta-border, #d1d5db)",
        marginBottom: 4,
      }}
    >
      {tabs.map((t) => {
        const isActive = t.id === active;
        return (
          <button
            key={t.id}
            type="button"
            role="tab"
            aria-selected={isActive}
            onClick={() => onChange(t.id)}
            style={{
              padding: "6px 12px",
              border: "none",
              background: isActive ? "var(--voitta-accent-tint, #eef2ff)" : "transparent",
              color: "var(--voitta-text)",
              borderBottom: isActive ? "2px solid var(--voitta-accent, #4f46e5)" : "2px solid transparent",
              cursor: "pointer",
              fontWeight: isActive ? 600 : 400,
              fontSize: 13,
            }}
          >
            {t.label}
          </button>
        );
      })}
    </div>
  );
}

function PluginTabBody({
  pluginName,
  plugin,
  backendOrigin,
  onRefresh,
  refreshBusy,
}: {
  pluginName: string;
  plugin: PluginInfo | undefined;
  backendOrigin: string;
  onRefresh: () => void;
  refreshBusy: boolean;
}) {
  if (!plugin) {
    return <p className="muted">Plugin {pluginName} no longer loaded; switch to another tab.</p>;
  }
  // Custom panel beats schema if both are present — the documented
  // opt-out escape hatch.
  const CustomPanel = findCustomPanel(plugin.rel_dir ?? plugin.name);
  if (CustomPanel) {
    return <CustomPanel pluginName={plugin.name} backendOrigin={backendOrigin} />;
  }
  if (plugin.settings_schema) {
    return (
      <PluginSettingsPanel
        pluginName={plugin.name}
        schema={plugin.settings_schema}
        connectors={plugin.mcp_connectors}
        backendOrigin={backendOrigin}
        onRefreshConnectors={onRefresh}
        refreshBusy={refreshBusy}
        hosts={[
          ...plugin.host_patterns.filter((h) => !h.includes("*")),
          ...(plugin.extra_hosts ?? []),
        ]}
      />
    );
  }
  return <p className="muted">This plugin has no configurable settings.</p>;
}

function LoadedPluginsFooter({
  plugins,
  loadErr,
}: {
  plugins: PluginInfo[];
  loadErr: string | null;
}) {
  if (loadErr) {
    return (
      <p className="muted" style={{ marginTop: 16, color: "#b00020", fontSize: 11 }}>
        Couldn't fetch plugin catalogue: {loadErr}
      </p>
    );
  }
  if (plugins.length === 0) return null;
  return (
    <details style={{ marginTop: 16 }}>
      <summary className="muted" style={{ cursor: "pointer", fontSize: 12 }}>
        Loaded plugins ({plugins.length})
      </summary>
      <ul style={{ margin: "6px 0 0", padding: "0 0 0 16px", fontSize: 12 }}>
        {plugins.map((p) => (
          <li key={p.name}>
            <code>{p.name}</code>
            {p.version && <span className="muted"> v{p.version}</span>}
            {p.host_patterns.length > 0 && (
              <span className="muted"> · {p.host_patterns.join(", ")}</span>
            )}
            {p.mcp_connectors.length > 0 && (
              <span className="muted">
                {" · "}MCP: {p.mcp_connectors.map((c) => `${c.id} (${c.status})`).join(", ")}
              </span>
            )}
          </li>
        ))}
      </ul>
    </details>
  );
}
