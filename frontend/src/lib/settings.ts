// Settings cache + bootstrap. Per-provider api_keys + models so
// switching providers in the UI doesn't blow away the previous key.
// Keys are write-only on the wire — GET returns booleans only.

export type Layout = "chat-right" | "chat-left";
export type Theme = "light" | "dark" | "auto";
export type ProviderId = "anthropic" | "openai" | "gemini" | "requesty" | "claude_code";

// The 4th "brain": the Claude Agent SDK driven by a Pro/Max subscription.
// Selected via the same provider dropdown but handled by a separate runtime
// (no API key — subscription token collected in-chat).
export const AGENT_SDK_PROVIDER: ProviderId = "claude_code";

export interface AgentSdkStatus {
  available: boolean; // Claude Code engine installed on the machine?
  has_token: boolean; // subscription token stored for this user?
}

export interface GoogleOAuthBlob {
  // The wire shape is whatever the BE saved minus ``tokens`` (redacted
  // server-side). Today the relevant fields are ``clientId`` /
  // ``clientSecret`` but more may appear over time, so we keep the
  // shape open.
  clientId?: string;
  clientSecret?: string;
  [k: string]: unknown;
}

export interface PublicSettings {
  provider: ProviderId;
  models: Record<string, string>;
  layout: Layout;
  theme: Theme;
  max_tool_iterations: number;
  max_tokens: number;
  has_api_keys: Record<string, boolean>;
  // Nested slices for the per-plugin Settings tabs. Plugin panels read
  // their saved fields out of ``plugins[name]`` (which the BE projects
  // from the dotted-path namespace ``plugins.<name>.<...>``). The
  // Google panel reads ``googleOAuth`` separately because the OAuth
  // flow predates the plugin-settings system.
  googleOAuth: GoogleOAuthBlob;
  plugins: Record<string, Record<string, unknown>>;
  agent_sdk: AgentSdkStatus;
}

export interface SettingsPatch {
  provider?: ProviderId;
  api_keys?: Record<string, string>;
  models?: Record<string, string>;
  layout?: Layout;
  theme?: Theme;
  max_tool_iterations?: number;
  max_tokens?: number;
  // Dotted-path patches: ``"plugins.voitta-enterprise.mcp.url": "http://…"``.
  // Empty string or null DELETES the key (server-side semantics).
  dotted?: Record<string, unknown>;
}

// No hardcoded model list. ``models`` holds only the user's *pinned*
// selection per provider (empty = "use the catalog default"). The list of
// choices and the default both come from GET /api/models/{provider}
// (see lib/models.ts), which serves live data or the bundled snapshot.
const DEFAULT: PublicSettings = {
  provider: "anthropic",
  models: {},
  layout: "chat-right",
  theme: "auto",
  max_tool_iterations: 25,
  max_tokens: 24576,
  has_api_keys: {},
  googleOAuth: {},
  plugins: {},
  agent_sdk: { available: false, has_token: false },
};

let cache: PublicSettings = { ...DEFAULT };
const subscribers = new Set<(s: PublicSettings) => void>();

export function getSettings(): PublicSettings {
  return cache;
}

export function subscribeSettings(fn: (s: PublicSettings) => void): () => void {
  subscribers.add(fn);
  return () => {
    subscribers.delete(fn);
  };
}

function notify() {
  for (const fn of subscribers) fn(cache);
}

function normalise(s: Partial<PublicSettings>): PublicSettings {
  return {
    provider: (s.provider as ProviderId) ?? DEFAULT.provider,
    models: { ...(s.models ?? {}) },
    layout: (s.layout as Layout) ?? DEFAULT.layout,
    theme: (s.theme as Theme) ?? DEFAULT.theme,
    max_tool_iterations:
      typeof s.max_tool_iterations === "number" && s.max_tool_iterations > 0
        ? s.max_tool_iterations
        : DEFAULT.max_tool_iterations,
    max_tokens:
      typeof s.max_tokens === "number" && s.max_tokens > 0
        ? s.max_tokens
        : DEFAULT.max_tokens,
    has_api_keys: s.has_api_keys ?? {},
    googleOAuth: (s.googleOAuth ?? {}) as GoogleOAuthBlob,
    plugins: (s.plugins ?? {}) as Record<string, Record<string, unknown>>,
    agent_sdk: {
      available: Boolean(s.agent_sdk?.available),
      has_token: Boolean(s.agent_sdk?.has_token),
    },
  };
}

/** Apply ``"a.b.c": v`` to the in-memory cache. Empty-string / null
 * deletes the key (matches BE semantics). Only the redacted slices
 * (``googleOAuth``, ``plugins``) are touched; typed top-level fields
 * have their own dedicated patch fields. */
function applyDottedToCache(
  next: PublicSettings,
  path: string,
  value: unknown,
): void {
  const parts = path.split(".");
  if (parts.length < 1) return;
  // We only mirror the slices the redacted GET returns. Other dotted
  // keys (e.g. tokens) live server-side only.
  if (parts[0] !== "plugins" && parts[0] !== "googleOAuth") return;
  const root = (next as unknown as Record<string, unknown>)[parts[0]] as
    | Record<string, unknown>
    | undefined;
  if (!root) return;
  let cur: Record<string, unknown> = root;
  for (let i = 1; i < parts.length - 1; i++) {
    const k = parts[i];
    const nxt = cur[k];
    if (!nxt || typeof nxt !== "object") {
      const obj: Record<string, unknown> = {};
      cur[k] = obj;
      cur = obj;
    } else {
      cur = nxt as Record<string, unknown>;
    }
  }
  const leaf = parts[parts.length - 1];
  if (value === "" || value === null || value === undefined) {
    delete cur[leaf];
  } else {
    cur[leaf] = value;
  }
}


/** Walk a dotted path through a nested-object blob. Returns ``undefined``
 * for any miss. Pure read; doesn't mutate. */
export function getDotted(
  blob: Record<string, unknown>,
  path: string,
): unknown {
  const parts = path.split(".");
  let cur: unknown = blob;
  for (const p of parts) {
    if (cur && typeof cur === "object" && p in (cur as object)) {
      cur = (cur as Record<string, unknown>)[p];
    } else {
      return undefined;
    }
  }
  return cur;
}

export async function bootstrapSettings(backendOrigin: string): Promise<PublicSettings> {
  try {
    const res = await fetch(`${backendOrigin}/api/settings`, { credentials: "include" });
    if (res.ok) {
      cache = normalise((await res.json()) as Partial<PublicSettings>);
      notify();
    }
  } catch (err) {
    console.warn("[voitta] settings bootstrap failed", err);
  }
  return cache;
}

export async function saveSettings(
  backendOrigin: string,
  patch: SettingsPatch,
): Promise<PublicSettings> {
  // Optimistic local update so the UI reflects the change before the
  // round-trip lands. api_keys → has_api_keys (we never cache the
  // plaintext).
  const next: PublicSettings = {
    ...cache,
    provider: patch.provider ?? cache.provider,
    layout: patch.layout ?? cache.layout,
    theme: patch.theme ?? cache.theme,
    max_tool_iterations: patch.max_tool_iterations ?? cache.max_tool_iterations,
    max_tokens: patch.max_tokens ?? cache.max_tokens,
    models: { ...cache.models, ...(patch.models ?? {}) },
    has_api_keys: { ...cache.has_api_keys },
    googleOAuth: { ...cache.googleOAuth },
    plugins: { ...cache.plugins },
    agent_sdk: { ...cache.agent_sdk },
  };
  if (patch.api_keys) {
    for (const [p, v] of Object.entries(patch.api_keys)) {
      if (v === "") delete next.has_api_keys[p];
      else next.has_api_keys[p] = true;
    }
  }
  // Apply dotted patches to the local cache too so plugin panels
  // see the optimistic update without a server round-trip.
  if (patch.dotted) {
    for (const [path, value] of Object.entries(patch.dotted)) {
      applyDottedToCache(next, path, value);
    }
  }
  cache = next;
  notify();

  const res = await fetch(`${backendOrigin}/api/settings`, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(patch),
    credentials: "include",
  });
  if (res.ok) {
    cache = normalise((await res.json()) as Partial<PublicSettings>);
    notify();
  }
  return cache;
}
