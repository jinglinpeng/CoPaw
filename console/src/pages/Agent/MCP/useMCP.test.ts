import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import type { MCPClientInfo } from "../../../api/types";

const hoisted = vi.hoisted(() => {
  const messageMock = {
    success: vi.fn(),
    error: vi.fn(),
  };
  const apiMocks = {
    listMCPClients: vi.fn(),
    createMCPClient: vi.fn(),
    updateMCPClient: vi.fn(),
    toggleMCPClient: vi.fn(),
    deleteMCPClient: vi.fn(),
  };
  // A stable translation function so useCallback dependencies don't change on
  // every render and trigger an infinite loadClients loop via useEffect.
  const stableT = (k: string) => k;
  const agentState = {
    selectedAgent: "agent-1",
    agents: [] as {
      id: string;
      backend: string;
      backend_capabilities?: { provider_mcp_discovery: boolean };
    }[],
  };
  return { messageMock, apiMocks, stableT, agentState, discoverMCP: vi.fn() };
});

vi.mock("../../../api", () => ({
  __esModule: true,
  default: hoisted.apiMocks,
}));

vi.mock("../../../stores/agentStore", () => ({
  useAgentStore: () => hoisted.agentState,
}));

vi.mock("../../../api/modules/harness", () => ({
  harnessApi: { listMCP: hoisted.discoverMCP },
}));

vi.mock("../../../hooks/useAppMessage", () => ({
  useAppMessage: () => ({ message: hoisted.messageMock }),
}));

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: hoisted.stableT }),
}));

import { useMCP } from "./useMCP";

const { messageMock, apiMocks } = hoisted;

function makeClient(overrides: Partial<MCPClientInfo> = {}): MCPClientInfo {
  return {
    key: "client-1",
    name: "Client One",
    description: "desc",
    command: "cmd",
    enabled: false,
    transport: "stdio",
    ...overrides,
  } as MCPClientInfo;
}

describe("useMCP", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMocks.listMCPClients.mockReset();
    apiMocks.createMCPClient.mockReset();
    apiMocks.updateMCPClient.mockReset();
    apiMocks.toggleMCPClient.mockReset();
    apiMocks.deleteMCPClient.mockReset();
    messageMock.success.mockReset();
    messageMock.error.mockReset();

    apiMocks.listMCPClients.mockResolvedValue([]);
    hoisted.agentState.selectedAgent = "agent-1";
    hoisted.agentState.agents = [];
    vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("mounts and calls listMCPClients, sets clients, loading true->false", async () => {
    const clients = [makeClient(), makeClient({ key: "client-2" })];
    apiMocks.listMCPClients.mockResolvedValue(clients);

    const { result } = renderHook(() => useMCP());

    await waitFor(() => {
      expect(result.current.clients).toEqual(clients);
    });
    expect(result.current.loading).toBe(false);
    expect(apiMocks.listMCPClients).toHaveBeenCalledTimes(1);
  });

  it("message.error('mcp.loadError') when loadClients fails", async () => {
    apiMocks.listMCPClients.mockRejectedValue(new Error("network down"));

    const { result } = renderHook(() => useMCP());

    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });
    expect(messageMock.error).toHaveBeenCalledWith("mcp.loadError");
  });

  it("createClient success: calls createMCPClient with client_key + client, message.success, returns true", async () => {
    apiMocks.createMCPClient.mockResolvedValue(undefined);
    const { result } = renderHook(() => useMCP());
    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });

    let ret: boolean | undefined;
    await act(async () => {
      ret = await result.current.createClient("my-key", {
        name: "My",
        command: "run",
      });
    });

    expect(apiMocks.createMCPClient).toHaveBeenCalledWith({
      client_key: "my-key",
      client: { name: "My", command: "run" },
    });
    expect(messageMock.success).toHaveBeenCalledWith("mcp.createSuccess");
    expect(ret).toBe(true);
  });

  it("createClient failure with error.message: message.error receives error.message, returns false", async () => {
    apiMocks.createMCPClient.mockRejectedValue(new Error("dup key"));
    const { result } = renderHook(() => useMCP());
    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });

    let ret: boolean | undefined;
    await act(async () => {
      ret = await result.current.createClient("k", {
        name: "N",
        command: "c",
      });
    });

    expect(messageMock.error).toHaveBeenCalledWith("dup key");
    expect(ret).toBe(false);
  });

  it("createClient failure without error.message: message.error receives 'mcp.createError'", async () => {
    apiMocks.createMCPClient.mockRejectedValue({});
    const { result } = renderHook(() => useMCP());
    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });

    let ret: boolean | undefined;
    await act(async () => {
      ret = await result.current.createClient("k", {
        name: "N",
        command: "c",
      });
    });

    expect(messageMock.error).toHaveBeenCalledWith("mcp.createError");
    expect(ret).toBe(false);
  });

  it("updateClient success: calls updateMCPClient, message.success('mcp.updateSuccess'), returns true", async () => {
    apiMocks.updateMCPClient.mockResolvedValue(undefined);
    const { result } = renderHook(() => useMCP());
    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });

    let ret: boolean | undefined;
    await act(async () => {
      ret = await result.current.updateClient("client-1", { name: "Renamed" });
    });

    expect(apiMocks.updateMCPClient).toHaveBeenCalledWith("client-1", {
      name: "Renamed",
    });
    expect(messageMock.success).toHaveBeenCalledWith("mcp.updateSuccess");
    expect(ret).toBe(true);
  });

  it("toggleEnabled on enabled client: message.success('mcp.disableSuccess')", async () => {
    apiMocks.toggleMCPClient.mockResolvedValue(undefined);
    const { result } = renderHook(() => useMCP());
    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });

    await act(async () => {
      await result.current.toggleEnabled(makeClient({ enabled: true }));
    });

    expect(apiMocks.toggleMCPClient).toHaveBeenCalledWith("client-1");
    expect(messageMock.success).toHaveBeenCalledWith("mcp.disableSuccess");
  });

  it("deleteClient success: message.success('mcp.deleteSuccess')", async () => {
    apiMocks.deleteMCPClient.mockResolvedValue(undefined);
    const { result } = renderHook(() => useMCP());
    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });

    await act(async () => {
      await result.current.deleteClient(makeClient());
    });

    expect(apiMocks.deleteMCPClient).toHaveBeenCalledWith("client-1");
    expect(messageMock.success).toHaveBeenCalledWith("mcp.deleteSuccess");
  });

  it("polls pending status without page loading or provider discovery and stops on settlement", async () => {
    vi.useFakeTimers();
    const pending = makeClient({ enabled: true, runtime_status: "connecting" });
    let resolvePoll!: (clients: MCPClientInfo[]) => void;
    apiMocks.listMCPClients
      .mockResolvedValueOnce([pending])
      .mockImplementationOnce(
        () =>
          new Promise<MCPClientInfo[]>((resolve) => {
            resolvePoll = resolve;
          }),
      );
    const { result } = renderHook(() => useMCP());
    await act(async () => {});
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(apiMocks.listMCPClients).toHaveBeenCalledTimes(2);
    expect(result.current.loading).toBe(false);
    await act(async () => {
      resolvePoll([{ ...pending, runtime_status: "active" }]);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(6000);
    });
    expect(apiMocks.listMCPClients).toHaveBeenCalledTimes(2);
    expect(hoisted.discoverMCP).not.toHaveBeenCalled();
  });

  it("pauses when hidden and stops after unmount", async () => {
    vi.useFakeTimers();
    apiMocks.listMCPClients.mockResolvedValue([
      makeClient({ enabled: true, runtime_status: "connecting" }),
    ]);
    const { unmount } = renderHook(() => useMCP());
    await act(async () => {});
    const visibility = vi.spyOn(document, "visibilityState", "get");
    visibility.mockReturnValue("hidden");
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(6000);
    });
    expect(apiMocks.listMCPClients).toHaveBeenCalledTimes(1);
    visibility.mockReturnValue("visible");
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(apiMocks.listMCPClients).toHaveBeenCalledTimes(2);
    unmount();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(6000);
    });
    expect(apiMocks.listMCPClients).toHaveBeenCalledTimes(2);
  });

  it("discards an old agent's pending poll after switching agents", async () => {
    vi.useFakeTimers();
    const pending = makeClient({ enabled: true, runtime_status: "connecting" });
    const other = makeClient({
      key: "other",
      enabled: true,
      runtime_status: "active",
    });
    let resolveOld!: (clients: MCPClientInfo[]) => void;
    apiMocks.listMCPClients
      .mockResolvedValueOnce([pending])
      .mockImplementationOnce(
        () =>
          new Promise<MCPClientInfo[]>((resolve) => {
            resolveOld = resolve;
          }),
      )
      .mockResolvedValueOnce([other]);
    const { result, rerender } = renderHook(() => useMCP());
    await act(async () => {});
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    hoisted.agentState.selectedAgent = "agent-2";
    rerender();
    await act(async () => {});
    expect(result.current.clients).toEqual([other]);
    await act(async () => {
      resolveOld([pending]);
    });
    expect(result.current.clients).toEqual([other]);
    expect(result.current.loading).toBe(false);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(6000);
    });
    expect(apiMocks.listMCPClients).toHaveBeenCalledTimes(3);
  });

  it("does not poll third-party provider MCPs", async () => {
    vi.useFakeTimers();
    hoisted.agentState.agents = [
      {
        id: "agent-1",
        backend: "claude_code",
        backend_capabilities: { provider_mcp_discovery: true },
      },
    ];
    hoisted.discoverMCP.mockResolvedValue({ servers: [] });
    apiMocks.listMCPClients.mockResolvedValue([
      makeClient({ enabled: true, runtime_status: "connecting" }),
    ]);
    renderHook(() => useMCP());
    await act(async () => {});
    await act(async () => {
      await vi.advanceTimersByTimeAsync(6000);
    });
    expect(apiMocks.listMCPClients).toHaveBeenCalledTimes(1);
    expect(hoisted.discoverMCP).toHaveBeenCalledTimes(1);
  });
});
