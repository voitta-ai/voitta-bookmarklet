// Global tab — provider, API key, model, layout, theme.
//
// Extracted from the original single-tab SettingsView. Identical
// behaviour: per-provider api_keys + models so flipping providers
// preserves the saved key of the previous one (it reappears as a
// ``●●●● (saved)`` placeholder).

import { useEffect, useState } from "react";
import {
  AGENT_SDK_PROVIDER,
  bootstrapSettings,
  saveSettings,
  type Layout,
  type ProviderId,
  type SettingsPatch,
  type Theme,
} from "../lib/settings";
import {
  fetchModels,
  getCachedModels,
  type ModelCatalog,
} from "../lib/models";
import { useSettings } from "../lib/useSettings";

const PROVIDERS: { id: ProviderId; label: string }[] = [
  { id: "anthropic", label: "Anthropic (Claude)" },
  { id: "openai", label: "OpenAI (ChatGPT)" },
  { id: "gemini", label: "Google (Gemini)" },
  { id: "requesty", label: "Requesty (router)" },
  { id: "codex", label: "Codex (ChatGPT subscription)" },
];

const KEY_PLACEHOLDER: Record<ProviderId, string> = {
  anthropic: "sk-ant-...",
  openai: "sk-...",
  gemini: "AIza...",
  requesty: "sk-...",
  codex: "", // ChatGPT subscription via the codex CLI — no API key field
  claude_code: "", // subscription brain — no API key field
};

interface Props {
  backendOrigin: string;
}

export default function GlobalSettings({ backendOrigin }: Props) {
  const cached = useSettings();
  const [provider, setProvider] = useState<ProviderId>(cached.provider);
  const [models, setModels] = useState<Record<string, string>>(cached.models);
  const [layout, setLayout] = useState<Layout>(cached.layout);
  const [theme, setTheme] = useState<Theme>(cached.theme);
  const [maxToolIters, setMaxToolIters] = useState<number>(cached.max_tool_iterations);
  const [maxTokens, setMaxTokens] = useState<number>(cached.max_tokens);
  const [apiKey, setApiKey] = useState("");
  const [hasKey, setHasKey] = useState<Record<string, boolean>>(cached.has_api_keys);
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState<{ text: string; err: boolean } | null>(null);
  // Model catalog for the currently-selected provider. Seeded from the
  // module cache for an instant first paint, then revalidated over the wire.
  const [catalog, setCatalog] = useState<ModelCatalog>(() => getCachedModels(provider));
  const [modelsLoading, setModelsLoading] = useState(false);

  async function loadModels(p: ProviderId, opts: { force?: boolean } = {}) {
    setModelsLoading(true);
    // Show whatever we already have cached for this provider immediately.
    setCatalog(getCachedModels(p));
    try {
      const next = await fetchModels(backendOrigin, p, opts);
      // Guard against a stale response if the provider changed mid-flight.
      setProvider((cur) => {
        if (cur === p) setCatalog(next);
        return cur;
      });
    } finally {
      setModelsLoading(false);
    }
  }

  // Clear the in-flight unsaved key when switching providers so a
  // half-pasted key for OpenAI doesn't end up saved under Anthropic.
  useEffect(() => setApiKey(""), [provider]);

  // Fetch the catalog whenever the selected provider changes (also fires on
  // mount / panel-open). Cache-first on the backend, so this is cheap.
  useEffect(() => {
    void loadModels(provider);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [provider]);

  useEffect(() => {
    setProvider(cached.provider);
    setModels(cached.models);
    setLayout(cached.layout);
    setTheme(cached.theme);
    setMaxToolIters(cached.max_tool_iterations);
    setMaxTokens(cached.max_tokens);
    setHasKey(cached.has_api_keys);
  }, [cached]);

  async function onSave() {
    setSaving(true);
    setStatus(null);
    const patch: SettingsPatch = {
      provider, layout, theme,
      max_tool_iterations: maxToolIters,
      max_tokens: maxTokens,
    };
    if (models[provider]) patch.models = { [provider]: models[provider] };
    if (apiKey) patch.api_keys = { [provider]: apiKey };
    const keyWasSet = Boolean(apiKey);
    try {
      await saveSettings(backendOrigin, patch);
      setApiKey("");
      setStatus({ text: "Saved.", err: false });
      // A newly-saved key means the backend can now fetch a live catalog —
      // force past the TTL so the dropdown updates immediately.
      if (keyWasSet) void loadModels(provider, { force: true });
    } catch (err) {
      setStatus({ text: `Error: ${err}`, err: true });
    } finally {
      setSaving(false);
    }
  }

  async function onClearKey() {
    setSaving(true);
    try {
      await saveSettings(backendOrigin, { api_keys: { [provider]: "" } });
      setStatus({ text: "Key cleared.", err: false });
      // No credential now → catalog reverts to the bundled snapshot.
      void loadModels(provider, { force: true });
    } catch (err) {
      setStatus({ text: `Error: ${err}`, err: true });
    } finally {
      setSaving(false);
    }
  }

  const choices = catalog.models;
  const model = models[provider] ?? "";
  // A pinned model the provider no longer offers (renamed / retired). We keep
  // the pin selectable but warn, so the user can consciously switch.
  const pinnedButGone = Boolean(model) && choices.length > 0 && !choices.includes(model);
  const effectiveDefault = catalog.default || choices[0] || "";
  const providerHasKey = Boolean(hasKey[provider]);
  const isBrain = provider === AGENT_SDK_PROVIDER;
  // The subscription brain is only offered when the Claude Code engine is
  // installed on this machine (Phase 4 gating).
  const providerOptions = cached.agent_sdk.available
    ? [...PROVIDERS, { id: AGENT_SDK_PROVIDER, label: "Claude (subscription)" }]
    : PROVIDERS;

  async function onDisconnectToken() {
    setSaving(true);
    try {
      await fetch(`${backendOrigin}/api/agent_sdk/disconnect`, {
        method: "POST",
        credentials: "include",
      });
      await bootstrapSettings(backendOrigin); // refresh has_token
      // Backend invalidated the claude_code catalog on disconnect; reload so
      // the dropdown reflects the snapshot instead of a stale list.
      void loadModels(provider, { force: true });
      setStatus({ text: "Disconnected.", err: false });
    } catch (err) {
      setStatus({ text: `Error: ${err}`, err: true });
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="view-settings global-settings">
      <label htmlFor="vb-provider">Provider</label>
      <select
        id="vb-provider"
        value={provider}
        onChange={(e) => setProvider(e.target.value as ProviderId)}
      >
        {providerOptions.map((p) => (
          <option key={p.id} value={p.id}>
            {p.label}
            {p.id === AGENT_SDK_PROVIDER
              ? cached.agent_sdk.has_token
                ? " ✓"
                : ""
              : hasKey[p.id]
                ? " ✓"
                : ""}
          </option>
        ))}
      </select>
      <p className="muted">You can keep keys for all providers and switch any time.</p>

      {isBrain ? (
        // Subscription brain: no API key. Auth is a one-time token collected
        // in chat (claude setup-token) the first time you send a message.
        <div className="agent-sdk-auth" style={{ marginTop: 4 }}>
          {cached.agent_sdk.has_token ? (
            <div className="muted">
              ✓ Claude subscription connected.{" "}
              <button
                type="button"
                className="link-btn"
                onClick={onDisconnectToken}
                disabled={saving}
              >
                Disconnect
              </button>
              .
            </div>
          ) : (
            <p className="muted">
              No API key needed — uses your Claude Pro/Max subscription. The
              first time you send a message I'll walk you through pasting a
              one-time token (<code>claude setup-token</code>). It's stored
              locally and never shown in chat.
            </p>
          )}
        </div>
      ) : provider === "codex" ? (
        // ChatGPT subscription: credentials are the codex CLI's own
        // ~/.codex/auth.json, re-read on every request.
        <p className="muted" style={{ marginTop: 4 }}>
          {providerHasKey
            ? "✓ Signed in through the codex CLI."
            : "Not signed in. Run `codex login` in a terminal."}{" "}
          No API key needed — uses your ChatGPT plan via the <code>codex</code> CLI's login.
        </p>
      ) : (
        <>
          <label htmlFor="vb-key">API key</label>
          <input
            id="vb-key"
            type="text"
            className="secret"
            value={apiKey}
            placeholder={
              providerHasKey
                ? "●●●●●●●●●●●●●●●●  (saved — type to replace)"
                : KEY_PLACEHOLDER[provider]
            }
            onChange={(e) => setApiKey(e.target.value)}
            autoComplete="off"
            spellCheck={false}
          />
          {providerHasKey && (
            <div className="muted">
              A key for {provider} is saved on disk.{" "}
              <button
                type="button"
                className="link-btn"
                onClick={onClearKey}
                disabled={saving}
              >
                Clear it
              </button>
              .
            </div>
          )}
        </>
      )}

      <div className="model-label-row" style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
        <label htmlFor="vb-model" style={{ marginRight: "auto" }}>
          Model
        </label>
        <button
          type="button"
          className="link-btn"
          onClick={() => void loadModels(provider, { force: true })}
          disabled={modelsLoading}
          title="Re-fetch this provider's model list"
        >
          {modelsLoading ? "Refreshing…" : "↻ Refresh"}
        </button>
      </div>
      <select
        id="vb-model"
        value={model && choices.includes(model) ? model : model ? "_custom" : effectiveDefault}
        onChange={(e) => setModels({ ...models, [provider]: e.target.value })}
        disabled={modelsLoading && choices.length === 0}
      >
        {choices.length === 0 && (
          <option value="">{modelsLoading ? "Loading models…" : "No models available"}</option>
        )}
        {choices.map((m) => (
          <option key={m} value={m}>
            {m}
            {m === effectiveDefault ? "  (default)" : ""}
          </option>
        ))}
        {pinnedButGone && <option value="_custom">custom: {model}</option>}
      </select>
      <p className="muted" style={{ marginTop: 4 }}>
        {catalog.source === "snapshot"
          ? isBrain
            ? "Built-in list (the subscription engine doesn't expose a live model list)."
            : providerHasKey
              ? "Offline list — couldn't reach the provider; showing the built-in snapshot."
              : "Built-in list. Add an API key to load this provider's live models."
          : catalog.source === "cache"
            ? "Cached list from a recent sync."
            : "Live list from the provider."}
        {pinnedButGone && (
          <>
            {" "}
            <span className="err">
              Your pinned model “{model}” is no longer offered — pick another or it falls back to{" "}
              {effectiveDefault || "the default"}.
            </span>
          </>
        )}
      </p>

      <label>Layout</label>
      <div className="radio-row">
        <label className="radio">
          <input
            type="radio"
            name="layout"
            checked={layout === "chat-right"}
            onChange={() => setLayout("chat-right")}
          />
          chat-right
        </label>
        <label className="radio">
          <input
            type="radio"
            name="layout"
            checked={layout === "chat-left"}
            onChange={() => setLayout("chat-left")}
          />
          chat-left
        </label>
      </div>

      <label htmlFor="vb-theme">Theme</label>
      <select
        id="vb-theme"
        value={theme}
        onChange={(e) => setTheme(e.target.value as Theme)}
      >
        <option value="auto">Auto (follow OS)</option>
        <option value="light">Light</option>
        <option value="dark">Dark</option>
      </select>

      <label htmlFor="vb-max-tokens">Max response tokens per turn</label>
      <input
        id="vb-max-tokens"
        type="number"
        min={256}
        max={200000}
        step={1024}
        value={maxTokens}
        onChange={(e) => setMaxTokens(Math.max(256, Number(e.target.value) || 256))}
      />
      <p className="muted">
        Per-call response cap sent to the LLM as max_tokens. When the model
        hits it, the response truncates and you'll see a ⚠️ note explaining
        why. Default 24576 (~24K).
      </p>

      <label htmlFor="vb-max-tool-iters">Max tool-use iterations per turn</label>
      <input
        id="vb-max-tool-iters"
        type="number"
        min={1}
        max={200}
        value={maxToolIters}
        onChange={(e) => setMaxToolIters(Math.max(1, Number(e.target.value) || 1))}
      />
      <p className="muted">
        Hard cap on tool calls the agent can chain in a single turn before
        the loop aborts. Raise if you see ⚠️ tool-use loop exceeded warnings.
      </p>

      <div className="actions">
        <button
          className="save-btn"
          type="button"
          onClick={onSave}
          disabled={saving}
        >
          {saving ? "Saving…" : "Save"}
        </button>
        {status && (
          <span className={`status ${status.err ? "err" : ""}`}>{status.text}</span>
        )}
      </div>
    </div>
  );
}
