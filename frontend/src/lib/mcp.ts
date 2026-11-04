import type { PluginConnectorStatus } from "../settings/types";

export interface MCPBinding {
  plugin: string;
  enabled: boolean;
  has_token: boolean;
}

export interface MCPServer {
  id: string;
  name: string;
  url: string | null;
  url_template: string | null;
  tool_prefix: string;
  transport: string;
  enabled: boolean;
  builtin: boolean;
  has_token: boolean;
  has_legacy_token?: boolean;
  bindings: MCPBinding[];
  connectors: (PluginConnectorStatus & { plugin: string })[];
}

export interface MCPCatalog {
  servers: MCPServer[];
}

export async function mcpRequest<T = MCPCatalog>(
  origin: string,
  path = "",
  method = "GET",
  body?: unknown,
): Promise<T> {
  const response = await fetch(`${origin}/api/mcp${path}`, {
    method,
    credentials: "include",
    ...(body === undefined ? {} : {
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  });
  if (!response.ok) {
    // Never echo a response body which might include rejected credentials.
    throw new Error(`MCP request failed (HTTP ${response.status}). Check the configuration and retry.`);
  }
  return response.json() as Promise<T>;
}

export function serverPath(id: string): string {
  return `/servers/${encodeURIComponent(id)}`;
}

export function tokenPatch(token: string, clear: boolean): { token?: string } {
  return clear ? { token: "" } : token.trim() ? { token: token.trim() } : {};
}
