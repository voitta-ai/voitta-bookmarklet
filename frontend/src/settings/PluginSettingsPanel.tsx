// Schema-driven settings panel for plugins.
//
// A plugin declares ``settings_schema`` in its manifest.json:
//
//   {
//     "title": "...",
//     "fields": [
//       { "key": "plugins.<name>.<...>",
//         "type": "text"|"url"|"secret"|"bool"|"enum",
//         "label": "...",
//         "default": "...",
//         "help": "...",
//         "placeholder": "...",
//         "options": [{"value":"a","label":"A"}, ...]   // enum only
//       },
//       ...
//     ],
//     "status_probe": "mcp:<connector_id>"   // optional
//   }
//
// The renderer walks the schema, reads current values via dotted-path
// lookup against the local settings cache, and writes back via the
// ``dotted`` patch field of ``saveSettings``. The status_probe block
// pulls connector status from /api/plugins and exposes a "Refresh tool
// list" button.

import { useEffect, useState } from "react";
import {
  getDotted,
  getSettings,
  saveSettings,
  subscribeSettings,
  type PublicSettings,
} from "../lib/settings";
import type {
  ConnectorEndpointStatus,
  PluginConnectorStatus,
  PluginSchema,
  PluginSchemaField,
} from "./types";

interface Props {
  pluginName: string;
  schema: PluginSchema;
  connectors: PluginConnectorStatus[];
  backendOrigin: string;
  onRefreshConnectors: () => void;
  refreshBusy: boolean;
  // Activation hosts (manifest patterns + user extras) — drives the
  // per-host rows of "secret_per_host" fields.
  hosts?: string[];
  // Per-host re-probe (the row's Connect/Refresh button) + its busy map
  // keyed by host.
  onRefreshHost?: (host: string) => void;
  hostBusy?: Record<string, boolean>;
}

export default function PluginSettingsPanel({
  pluginName,
  schema,
  connectors,
  backendOrigin,
  onRefreshConnectors,
  refreshBusy,
  hosts = [],
  onRefreshHost,
  hostBusy = {},
}: Props) {
  const [snapshot, setSnapshot] = useState<PublicSettings>(getSettings);
  useEffect(() => subscribeSettings(setSnapshot), []);

  function valueOf(field: PluginSchemaField): unknown {
    const v = getDotted(snapshot as unknown as Record<string, unknown>, field.key);
    return v === undefined ? field.default : v;
  }

  async function patchField(field: PluginSchemaField, raw: unknown) {
    await saveSettings(backendOrigin, { dotted: { [field.key]: raw } });
  }

  function savedFlag(field: PluginSchemaField): unknown {
    const parts = field.key.split(".");
    parts[parts.length - 1] = `has_${parts[parts.length - 1]}`;
    return getDotted(snapshot as unknown as Record<string, unknown>, parts.join("."));
  }

  // status_probe = "mcp:<connector_id>" — pull the matching connector.
  const probed = (() => {
    if (!schema.status_probe || !schema.status_probe.startsWith("mcp:")) return null;
    const cid = schema.status_probe.slice("mcp:".length);
    return connectors.find((c) => c.id === cid) || null;
  })();

  return (
    <div className="plugin-settings-panel">
      {schema.title && (
        <h3 style={{ margin: "0 0 12px", fontSize: 14 }}>{schema.title}</h3>
      )}
      {schema.fields.map((f) =>
        f.type === "secret_per_host" ? (
          <PerHostSecretRows
            key={f.key}
            field={f}
            hosts={hosts}
            value={valueOf(f)}
            saved={savedFlag(f)}
            onChange={(v) => patchField(f, v)}
            endpoints={probed?.endpoints ?? []}
            onRefreshHost={onRefreshHost}
            hostBusy={hostBusy}
          />
        ) : f.type === "secret" ? (
          <SecretEditor key={f.key} label={f.label} placeholder={f.placeholder}
            saved={!!savedFlag(f) || !!valueOf(f)} help={f.help}
            onSave={(v) => patchField(f, v)} />
        ) : (
          <FieldRow
            key={f.key}
            field={f}
            value={valueOf(f)}
            onChange={(v) => patchField(f, v)}
          />
        ),
      )}

      {probed && (
        <ConnectorStatusRow
          conn={probed}
          pluginName={pluginName}
          onRefresh={onRefreshConnectors}
          busy={refreshBusy}
          // Hosts already rendered as per-host rows above — the footer
          // only shows endpoints NOT covered there (explicit URLs).
          coveredHosts={
            schema.fields.some((f) => f.type === "secret_per_host") ? hosts : []
          }
        />
      )}
    </div>
  );
}

function FieldRow({
  field,
  value,
  onChange,
}: {
  field: PluginSchemaField;
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  const id = `pf-${field.key.replace(/[^a-zA-Z0-9]+/g, "-")}`;
  if (field.type === "bool") {
    return (
      <div style={{ marginTop: 12 }}>
        <label
          style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}
        >
          <input
            id={id}
            type="checkbox"
            checked={!!value}
            onChange={(e) => onChange(e.currentTarget.checked)}
          />
          <span>{field.label}</span>
        </label>
        {field.help && <p className="muted">{field.help}</p>}
      </div>
    );
  }
  if (field.type === "enum") {
    return (
      <div style={{ marginTop: 12 }}>
        <label htmlFor={id}>{field.label}</label>
        <select
          id={id}
          value={String(value ?? "")}
          onChange={(e) => onChange(e.currentTarget.value)}
        >
          {(field.options || []).map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
        {field.help && <p className="muted">{field.help}</p>}
      </div>
    );
  }
  // text | url | secret — same shape, ``secret`` masks visually via CSS.
  // We use type=text everywhere so Chrome doesn't pop the password manager.
  return (
    <div style={{ marginTop: 12 }}>
      <label htmlFor={id}>{field.label}</label>
      <input
        id={id}
        className={field.type === "secret" ? "secret" : undefined}
        type="text"
        spellCheck={false}
        autoComplete="off"
        autoCorrect="off"
        autoCapitalize="off"
        placeholder={field.placeholder}
        value={String(value ?? "")}
        onChange={(e) => onChange(e.currentTarget.value)}
      />
      {field.help && <p className="muted">{field.help}</p>}
    </div>
  );
}

/** "https://x.y:8756/mcp" / "x.y:8756" → "x.y:8756". */
function hostOf(urlOrHost: string): string {
  let s = urlOrHost.trim();
  const i = s.indexOf("://");
  if (i !== -1) s = s.slice(i + 3);
  return s.split("/")[0].toLowerCase();
}

function PerHostSecretRows({
  field,
  hosts,
  value,
  saved,
  onChange,
  endpoints,
  onRefreshHost,
  hostBusy,
}: {
  field: PluginSchemaField;
  hosts: string[];
  value: unknown;
  saved: unknown;
  onChange: (v: unknown) => Promise<void>;
  endpoints: ConnectorEndpointStatus[];
  onRefreshHost?: (host: string) => void;
  hostBusy: Record<string, boolean>;
}) {
  const map: Record<string, string> =
    value && typeof value === "object" ? { ...(value as Record<string, string>) } : {};
  // Show a row per activation host, plus any saved key whose host was
  // since removed from the activation list (so it stays deletable).
  const flags: Record<string, boolean> = saved && typeof saved === "object"
    ? saved as Record<string, boolean> : {};
  const rows = [...hosts];
  for (const h of [...Object.keys(map), ...Object.keys(flags)]) {
    if (!rows.includes(h)) rows.push(h);
  }

  async function setKey(host: string, raw: string) {
    // Redacted MCP maps accept a partial host patch, preserving every
    // other host's secret. Non-MCP legacy maps still use full replacement.
    if (saved !== undefined) {
      await onChange({ [host]: raw });
    } else {
      const next = { ...map };
      if (raw.trim() === "") delete next[host];
      else next[host] = raw;
      await onChange(Object.keys(next).length ? next : null);
    }
  }

  if (rows.length === 0) {
    return (
      <p className="muted" style={{ marginTop: 12, fontSize: 12 }}>
        {field.label}: add an activation host above to configure a key.
      </p>
    );
  }
  return (
    <div style={{ marginTop: 12 }}>
      <label>{field.label}</label>
      {rows.map((host) => {
        const ep = endpoints.find((e) => hostOf(e.url) === hostOf(host));
        const busy = !!hostBusy[host];
        return (
          <div
            key={host}
            style={{
              marginTop: 8,
              padding: "8px 10px",
              border: "1px solid var(--voitta-border, #d1d5db)",
              borderRadius: 6,
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <code
                style={{ fontSize: 11, width: 170, flexShrink: 0, overflow: "hidden", textOverflow: "ellipsis" }}
                title={host}
              >
                {host}
              </code>
              <SecretEditor label={`${field.label} for ${host}`} saved={!!flags[host] || !!map[host]}
                placeholder={field.placeholder} onSave={(value) => setKey(host, value)} />
              {onRefreshHost && (
                <button
                  type="button"
                  className="save-btn"
                  disabled={busy}
                  onClick={() => onRefreshHost(host)}
                  style={{ background: "#6b7280", padding: "4px 10px", fontSize: 12, flexShrink: 0 }}
                >
                  {busy ? "Connecting…" : ep ? "Refresh" : "Connect"}
                </button>
              )}
            </div>
            <EndpointStatusLine ep={ep} />
          </div>
        );
      })}
      {field.help && <p className="muted">{field.help}</p>}
    </div>
  );
}

function SecretEditor({ label, saved, placeholder, help, onSave }: {
  label: string;
  saved: boolean;
  placeholder?: string;
  help?: string;
  onSave: (value: string) => Promise<void>;
}) {
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function persist(value: string) {
    setBusy(true); setError("");
    try { await onSave(value); setDraft(""); }
    catch { setError("Could not save the token. Please retry."); }
    finally { setBusy(false); }
  }
  return (
    <div style={{ flex: 1, minWidth: 0, marginTop: 8 }}>
      <label style={{ display: "grid", gap: 4, fontSize: 12 }}>{label}
        <input type="text" className="secret" autoComplete="off" autoCorrect="off" autoCapitalize="off" spellCheck={false}
          value={draft} disabled={busy}
          placeholder={saved ? "Token saved — leave blank to keep" : placeholder}
          onChange={(e) => setDraft(e.target.value)} />
      </label>
      <div style={{ display: "flex", gap: 6, marginTop: 4 }}>
        <button type="button" disabled={busy || !draft.trim()} onClick={() => void persist(draft.trim())}>Save token</button>
        {saved && <button type="button" disabled={busy} onClick={() => void persist("")}>Clear token</button>}
      </div>
      {help && <p className="muted">{help}</p>}
      {error && <p role="alert">{error}</p>}
    </div>
  );
}

function EndpointStatusLine({ ep }: { ep: ConnectorEndpointStatus | undefined }) {
  if (!ep) {
    return (
      <p className="muted" style={{ margin: "6px 0 0", fontSize: 11 }}>
        · not probed yet
      </p>
    );
  }
  const dot = STATUS_DOTS[ep.status] ?? "•";
  const label = STATUS_LABELS[ep.status] ?? ep.status;
  // Bound to ~6 names so a 14-tool server doesn't blow the card up.
  const preview = ep.tool_names.slice(0, 6).join(", ");
  const more =
    ep.tool_names.length > 6 ? `, +${ep.tool_names.length - 6} more` : "";
  return (
    <div style={{ margin: "6px 0 0", fontSize: 11 }}>
      <span aria-hidden>{dot}</span>{" "}
      <code>{ep.url}</code>{" "}
      <span className="muted">
        — {ep.status === "ok" ? `${ep.tool_count} ${ep.tool_count === 1 ? "tool" : "tools"}` : label}
      </span>
      {ep.status === "ok" && preview && (
        <p className="muted" style={{ margin: "2px 0 0" }}>
          {preview}{more}
        </p>
      )}
      {ep.last_error && (
        <p className="muted" style={{ margin: "2px 0 0", color: "#b00020" }}>
          {ep.last_error}
        </p>
      )}
    </div>
  );
}

const STATUS_DOTS: Record<string, string> = {
  ok: "●",
  unauth: "✕",
  unreachable: "⚠",
  not_configured: "○",
  unknown: "·",
};

const STATUS_LABELS: Record<string, string> = {
  ok: "Connected",
  unauth: "Unauthorized — token rejected",
  unreachable: "Unreachable",
  not_configured: "Not configured",
  unknown: "Not probed yet",
  disabled: "Disabled",
};

function ConnectorStatusRow({
  conn,
  pluginName,
  onRefresh,
  busy,
  coveredHosts,
}: {
  conn: PluginConnectorStatus;
  pluginName: string;
  onRefresh: () => void;
  busy: boolean;
  coveredHosts: string[];
}) {
  const covered = new Set(coveredHosts.map(hostOf));
  // Per-host rows above already show their endpoint inline; only
  // endpoints outside the activation list (explicit URLs) remain here.
  const extraEndpoints = (conn.endpoints ?? []).filter(
    (ep) => !covered.has(hostOf(ep.url)),
  );
  const dot = STATUS_DOTS[conn.status] ?? "•";
  const label = STATUS_LABELS[conn.status] ?? conn.status;
  return (
    <div
      style={{
        marginTop: 16,
        padding: "10px 12px",
        border: "1px solid var(--voitta-border, #d1d5db)",
        borderRadius: 6,
        background: "var(--voitta-accent-tint, #f9fafb)",
      }}
    >
      {extraEndpoints.map((ep) => (
        <div key={ep.url} style={{ marginBottom: 6 }}>
          <EndpointStatusLine ep={ep} />
        </div>
      ))}
      {extraEndpoints.length === 0 && conn.endpoints?.length === 0 && (
        // No probe results yet (not_configured / pre-refresh) — show
        // the aggregate badge so the card is never empty.
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span aria-hidden style={{ fontSize: 14 }}>{dot}</span>
          <strong>{label}</strong>
        </div>
      )}
      {conn.url_template && (
        <p className="muted" style={{ margin: "6px 0 0", fontSize: 11 }}>
          endpoint is derived from the page host per call (
          <code>{conn.url_template}</code>)
        </p>
      )}
      <div style={{ marginTop: 8, display: "flex", gap: 8 }}>
        <button
          type="button"
          onClick={onRefresh}
          disabled={busy}
          className="save-btn"
          style={{ background: "#6b7280", padding: "4px 10px", fontSize: 12 }}
        >
          {busy ? "Refreshing…" : "Refresh all"}
        </button>
        <span className="muted" style={{ fontSize: 11, alignSelf: "center" }}>
          plugin <code>{pluginName}</code>
        </span>
      </div>
    </div>
  );
}
