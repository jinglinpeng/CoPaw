import { describe, expect, it } from "vitest";
import {
  buildMcpSlashSuggestions,
  getMessageMcpIds,
  normalizeMcpMessage,
  parseMcpSelection,
} from "./mcpSlash";
import {
  getDraftStorageKey,
  parseDraft,
  serializeDraft,
} from "./chatInputDraft";

describe("explicit MCP selection", () => {
  it("uses the stable key, distinguishes skills, and lists only enabled servers", () => {
    expect(
      buildMcpSlashSuggestions([
        { key: "key one", name: "Skill Name", description: "", enabled: true },
        { key: "disabled", name: "Hidden", description: "", enabled: false },
      ]),
    ).toEqual([{ label: "/Skill Name · MCP", value: "mcp:key%20one" }]);
  });

  it("moves only the leading user selection into metadata without mutating input", () => {
    const message = {
      role: "user",
      content: [
        { type: "image", image_url: "image.png" },
        { type: "text", text: "/mcp:key%20one read /mcp:ordinary-text" },
      ],
      metadata: { client_message_id: "turn-1" },
    };
    const normalized = normalizeMcpMessage(message);
    expect(normalized).toEqual({
      ...message,
      content: [
        message.content[0],
        { type: "text", text: "read /mcp:ordinary-text" },
      ],
      metadata: { client_message_id: "turn-1", mcp_server_ids: ["key one"] },
    });
    expect(message.content[1].text).toMatch(/^\/mcp:/);
    expect(normalizeMcpMessage(normalized)).toBe(normalized);
    expect(getMessageMcpIds(message)).toEqual(getMessageMcpIds(normalized));
    expect(normalizeMcpMessage({ ...message, role: "assistant" }).content).toBe(
      message.content,
    );
    expect(
      getMessageMcpIds({
        role: "user",
        content: [{ type: "text", text: "next turn" }],
      }),
    ).toEqual([]);
  });

  it.each([
    "hello /mcp:echo",
    "/mcp:%broken",
    "/mcp:%0A",
    "/mcp:",
    "/skill task",
  ])("leaves ordinary or malformed text unchanged: %s", (text) => {
    expect(parseMcpSelection(text)).toBeNull();
    const message = { role: "user", content: [{ type: "text", text }] };
    expect(normalizeMcpMessage(message)).toBe(message);
  });

  it("round trips drafts and serialized queue content without separate selection state", () => {
    const draft = {
      value: "/mcp:echo queued task",
      selectionStart: 23,
      selectionEnd: 23,
    };
    const restored = parseDraft(serializeDraft(draft));
    expect(restored).toEqual(draft);
    expect(getDraftStorageKey("agent-a")).not.toBe(
      getDraftStorageKey("agent-b"),
    );
    const queued = JSON.parse(JSON.stringify({ text: restored!.value }));
    const submitted = normalizeMcpMessage({
      role: "user",
      content: [{ type: "text", text: queued.text }],
    });
    expect(getMessageMcpIds(submitted)).toEqual(["echo"]);
    expect(submitted.content[0].text).toBe("queued task");
  });
});
