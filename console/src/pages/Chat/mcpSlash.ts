import type { MCPClientSummary } from "../../api/types/mcp";

export interface McpSelection {
  serverId: string;
  raw: string;
  text: string;
}

export function parseMcpSelection(value: string): McpSelection | null {
  const match = /^\/mcp:([^\s]+)/.exec(value);
  if (!match) return null;
  try {
    const serverId = decodeURIComponent(match[1]);
    if (
      !serverId ||
      [...serverId].some(
        (char) => char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127,
      )
    )
      return null;
    return {
      serverId,
      raw: match[0],
      text: value.slice(match[0].length).replace(/^ /, ""),
    };
  } catch {
    return null;
  }
}

export function buildMcpSlashSuggestions(clients: MCPClientSummary[]) {
  return clients
    .filter((client) => client.enabled)
    .sort((a, b) => a.name.localeCompare(b.name))
    .map((client) => ({
      label: `/${client.name} · MCP`,
      value: `mcp:${encodeURIComponent(client.key)}`,
    }));
}

type MessageWithMcp = {
  role?: unknown;
  content?: unknown;
  metadata?: unknown;
};

export function normalizeMcpMessage<T extends MessageWithMcp>(message: T): T {
  if (message.role !== "user" || !Array.isArray(message.content)) {
    return message;
  }
  const index = message.content.findIndex((part) => part?.type === "text");
  const part = message.content[index];
  if (typeof part?.text !== "string") return message;
  const selection = parseMcpSelection(part.text);
  if (!selection) return message;
  const content = [...message.content];
  content[index] = { ...part, text: selection.text };
  return {
    ...message,
    content,
    metadata: {
      ...(message.metadata && typeof message.metadata === "object"
        ? message.metadata
        : {}),
      mcp_server_ids: [selection.serverId],
    },
  };
}

export function getMessageMcpIds(message: MessageWithMcp): string[] {
  const normalized = normalizeMcpMessage(message);
  const metadata = normalized.metadata as Record<string, unknown> | undefined;
  const ids = metadata?.mcp_server_ids;
  return Array.isArray(ids)
    ? [...new Set(ids.filter((id): id is string => typeof id === "string"))]
    : [];
}
