import { useCallback, useEffect, useRef, useState } from "react";
import { useAppMessage } from "../../../hooks/useAppMessage";
import api from "../../../api";
import type { MCPAccessPolicy, MCPClientInfo } from "../../../api/types";
import { useTranslation } from "react-i18next";
import { useAgentStore } from "../../../stores/agentStore";
import {
  harnessApi,
  type HarnessDiscoveredMCPServer,
} from "../../../api/modules/harness";

export function useMCP() {
  const { t } = useTranslation();
  const { selectedAgent, agents } = useAgentStore();
  const selectedAgentInfo = agents.find((item) => item.id === selectedAgent);
  const selectedBackend = selectedAgentInfo?.backend ?? "qwenpaw";
  const canDiscoverProviderMCP = Boolean(
    selectedAgentInfo?.backend_capabilities?.provider_mcp_discovery,
  );
  const [clients, setClients] = useState<MCPClientInfo[]>([]);
  const [providerServers, setProviderServers] = useState<
    HarnessDiscoveredMCPServer[]
  >([]);
  const [loading, setLoading] = useState(false);
  const [visible, setVisible] = useState(document.visibilityState !== "hidden");
  const mounted = useRef(false);
  const requestGeneration = useRef(0);
  const currentAgent = useRef(selectedAgent);
  currentAgent.current = selectedAgent;
  const { message } = useAppMessage();

  useEffect(() => {
    mounted.current = true;
    const onVisibility = () =>
      setVisible(document.visibilityState !== "hidden");
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      mounted.current = false;
      requestGeneration.current += 1;
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, []);

  const loadClients = useCallback(
    async (silent = false) => {
      const generation = ++requestGeneration.current;
      const isCurrent = () =>
        mounted.current &&
        generation === requestGeneration.current &&
        selectedAgent === currentAgent.current;
      if (!silent) setLoading(true);
      try {
        const data = await api.listMCPClients();
        if (!isCurrent() || (silent && document.visibilityState === "hidden"))
          return;
        setClients(data);
        if (silent) return;
        if (selectedBackend !== "qwenpaw" && canDiscoverProviderMCP) {
          try {
            const discovered = await harnessApi.listMCP(selectedBackend);
            if (!isCurrent()) return;
            setProviderServers(discovered.servers);
            if (discovered.message) {
              message.warning(discovered.message);
            }
          } catch (error) {
            if (!isCurrent()) return;
            console.warn("Failed to discover Provider MCP servers:", error);
            setProviderServers([]);
          }
        } else {
          setProviderServers([]);
        }
      } catch (error) {
        if (!isCurrent()) return;
        console.error("Failed to load MCP clients:", error);
        if (!silent) message.error(t("mcp.loadError"));
      } finally {
        if (!silent && isCurrent()) setLoading(false);
      }
    },
    [canDiscoverProviderMCP, message, selectedAgent, selectedBackend, t],
  );

  useEffect(() => {
    loadClients();
  }, [loadClients]);

  const hasPendingClients = clients.some(
    (client) => client.enabled && client.runtime_status === "connecting",
  );
  useEffect(() => {
    if (
      !visible ||
      loading ||
      !hasPendingClients ||
      selectedBackend !== "qwenpaw"
    )
      return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      await loadClients(true);
      if (!disposed) timer = setTimeout(poll, 2000);
    };
    timer = setTimeout(poll, 2000);
    return () => {
      disposed = true;
      clearTimeout(timer);
    };
  }, [hasPendingClients, loadClients, loading, selectedBackend, visible]);

  const createClient = useCallback(
    async (
      key: string,
      clientData: {
        name: string;
        description?: string;
        command: string;
        enabled?: boolean;
        transport?: "stdio" | "streamable_http" | "sse";
        url?: string;
        headers?: Record<string, string>;
        args?: string[];
        env?: Record<string, string>;
        cwd?: string;
      },
    ) => {
      try {
        await api.createMCPClient({
          client_key: key,
          client: clientData,
        });
        message.success(t("mcp.createSuccess"));
        await loadClients();
        return true;
      } catch (error: any) {
        const errorMsg = error?.message || t("mcp.createError");
        message.error(errorMsg);
        return false;
      }
    },
    [message, t, loadClients],
  );

  const updateClient = useCallback(
    async (
      key: string,
      updates: {
        name?: string;
        description?: string;
        command?: string;
        enabled?: boolean;
        transport?: "stdio" | "streamable_http" | "sse";
        url?: string;
        headers?: Record<string, string>;
        args?: string[];
        env?: Record<string, string>;
        cwd?: string;
      },
    ) => {
      try {
        await api.updateMCPClient(key, updates);
        message.success(t("mcp.updateSuccess"));
        await loadClients();
        return true;
      } catch (error: any) {
        const errorMsg = error?.message || t("mcp.updateError");
        message.error(errorMsg);
        return false;
      }
    },
    [message, t, loadClients],
  );

  const toggleEnabled = useCallback(
    async (client: MCPClientInfo) => {
      try {
        await api.toggleMCPClient(client.key);
        message.success(
          client.enabled ? t("mcp.disableSuccess") : t("mcp.enableSuccess"),
        );
        await loadClients();
      } catch (error) {
        message.error(t("mcp.toggleError"));
      }
    },
    [message, t, loadClients],
  );

  const deleteClient = useCallback(
    async (client: MCPClientInfo) => {
      try {
        await api.deleteMCPClient(client.key);
        message.success(t("mcp.deleteSuccess"));
        await loadClients();
      } catch (error) {
        message.error(t("mcp.deleteError"));
      }
    },
    [message, t, loadClients],
  );

  const updatePolicy = useCallback(
    async (clientKey: string, policy: MCPAccessPolicy) => {
      try {
        await api.updateMCPPolicy(clientKey, policy);
        message.success(t("mcp.access.saveSuccess"));
        await loadClients();
        return true;
      } catch (error: any) {
        const errorMsg = error?.message || t("mcp.access.saveError");
        message.error(errorMsg);
        return false;
      }
    },
    [message, t, loadClients],
  );

  return {
    clients,
    providerServers,
    loading,
    createClient,
    updateClient,
    updatePolicy,
    toggleEnabled,
    deleteClient,
    refreshClients: loadClients,
  };
}
