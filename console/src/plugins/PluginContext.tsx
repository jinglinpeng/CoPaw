/**
 * PluginContext.tsx
 *
 * Reactive plugin context for the host application.
 * Subscribes to the PluginSystem singleton and exposes plugin-registered
 * routes and tool renderers to any component via usePlugins().
 *
 *  const { toolRenderConfig, pluginRoutes, loading, error } = usePlugins();
 */

import React, { createContext, useContext, useEffect, useState } from "react";
import { pluginSystem } from "./hostExternals";
import { loadAllPlugins, PluginInitializationError } from "./usePluginLoader";
import { Alert, Button } from "antd";
import { useTranslation } from "react-i18next";
import type { PluginRouteDeclaration } from "./hostExternals";
import {
  routeRegistry,
  subscribe as registrySubscribe,
} from "./registry/store";

/** Derive the legacy PluginRouteDeclaration[] shape from routeRegistry. */
function derivePluginRoutes(): PluginRouteDeclaration[] {
  // Include both legacy (registerRoutes shim) routes and any new route.add
  // registrations from a plugin source. Built-in `core.*` routes are excluded.
  return routeRegistry
    .snapshot()
    .filter((r) => r.source !== "core")
    .map((r) => ({
      path: r.path,
      component: r.Component,
      label: r.id,
      icon: "",
    }));
}

// ─────────────────────────────────────────────────────────────────────────────
// Context shape
// ─────────────────────────────────────────────────────────────────────────────

export interface PluginContextValue {
  /** Map of tool-name → React component. Pass to `@agentscope-ai/chat`. */
  toolRenderConfig: Record<string, React.FC<any>>;
  /** Page routes registered by plugins. Inject into the router + sidebar. */
  pluginRoutes: PluginRouteDeclaration[];
  /** True while plugin initialization is running or waiting for the backend. */
  loading: boolean;
  /** Non-null if one or more plugins failed to load. */
  error: string | null;
}

const PluginContext = createContext<PluginContextValue>({
  toolRenderConfig: {},
  pluginRoutes: [],
  loading: true,
  error: null,
});

// ─────────────────────────────────────────────────────────────────────────────
// Provider
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Wrap your application root with `<PluginProvider>` once.
 * All descendants can then call `usePlugins()` to access plugin-registered
 * routes and tool renderers.
 */
export function PluginProvider({ children }: { children: React.ReactNode }) {
  const [toolRenderConfig, setToolRenderConfig] = useState<
    Record<string, React.FC<any>>
  >(pluginSystem.getToolRenderConfig());
  const [pluginRoutes, setPluginRoutes] = useState<PluginRouteDeclaration[]>(
    derivePluginRoutes(),
  );
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [retryKey, setRetryKey] = useState(0);
  const { t } = useTranslation();
  const retry = () => setRetryKey((key) => key + 1);

  useEffect(() => {
    let cancelled = false;
    let active: AbortController | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    let delay = 1000;
    setLoading(true);
    setError(null);
    // Re-sync state whenever any plugin registers new capabilities — both
    // the legacy pluginSystem (toolRenderers) and the new registry
    // (routes via shim + direct route.add) notify on change.
    const unsubA = pluginSystem.subscribe(() => {
      setToolRenderConfig(pluginSystem.getToolRenderConfig());
    });
    const unsubB = registrySubscribe(() => {
      setPluginRoutes(derivePluginRoutes());
    });

    const initialize = async (signal: AbortSignal) => {
      const [{ installHostSdk }, { registerBuiltinCards }] = await Promise.all([
        import("./hostSdk/install"),
        import("../components/Chat/ToolCards/registerBuiltinCards"),
      ]);
      signal.throwIfAborted();
      installHostSdk();
      registerBuiltinCards();
      return loadAllPlugins(signal);
    };

    const schedule = () => {
      if (cancelled) return;
      retryTimer = setTimeout(
        () => void attempt(),
        document.hidden ? 30000 : delay,
      );
      delay = Math.min(delay * 2, 5000);
    };
    const attempt = async () => {
      if (cancelled) return;
      const controller = new AbortController();
      active = controller;
      // A per-request liveness guard, not a startup deadline. A stalled
      // connection can be replaced without abandoning the mounted lifecycle.
      const timeout = setTimeout(
        () =>
          controller.abort(
            new DOMException("Plugin request timed out", "TimeoutError"),
          ),
        30000,
      );
      try {
        const summary = await initialize(controller.signal);
        if (cancelled || active !== controller) return;
        setError(summary.failed.length ? summary.failed.join("; ") : null);
        setLoading(Boolean(summary.retryable));
        if (summary.retryable) schedule();
      } catch (failure) {
        if (cancelled || active !== controller) return;
        const timedOut =
          controller.signal.reason instanceof DOMException &&
          controller.signal.reason.name === "TimeoutError";
        const error = timedOut
          ? new PluginInitializationError("Plugin request timed out", true)
          : failure;
        const retryable =
          error instanceof PluginInitializationError && error.retryable;
        const waiting =
          error instanceof PluginInitializationError && error.waiting;
        setError(
          waiting
            ? null
            : error instanceof Error
            ? error.message
            : "Plugin initialization failed",
        );
        setLoading(retryable);
        if (retryable) schedule();
      } finally {
        clearTimeout(timeout);
      }
    };
    const runWhenIdle = () => {
      void attempt();
    };
    // Browser/auth recovery signals restart this scope. Installed plugin
    // updates already use targeted reload functions; do not load them twice.
    const resume = () => setRetryKey((key) => key + 1);
    window.addEventListener("online", resume);
    window.addEventListener("focus", resume);
    window.addEventListener("qwenpaw:auth-changed", resume);

    const idleWindow = window as Window & {
      requestIdleCallback?: (callback: () => void) => number;
      cancelIdleCallback?: (handle: number) => void;
    };
    const handle = idleWindow.requestIdleCallback
      ? idleWindow.requestIdleCallback(runWhenIdle)
      : window.setTimeout(runWhenIdle, 1000);

    return () => {
      cancelled = true;
      active?.abort();
      clearTimeout(retryTimer);
      window.removeEventListener("online", resume);
      window.removeEventListener("focus", resume);
      window.removeEventListener("qwenpaw:auth-changed", resume);
      unsubA();
      unsubB();
      if (idleWindow.cancelIdleCallback) {
        idleWindow.cancelIdleCallback(handle);
      } else {
        window.clearTimeout(handle);
      }
    };
  }, [retryKey]);

  return (
    <PluginContext.Provider
      value={{ toolRenderConfig, pluginRoutes, loading, error }}
    >
      {error && (
        <Alert
          type="warning"
          showIcon
          message={t("pluginManager.loadFailed")}
          description={error}
          action={<Button onClick={retry}>{t("common.retry")}</Button>}
        />
      )}
      {children}
    </PluginContext.Provider>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Consumer hook
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Consume the global plugin context.
 *
 * ```tsx
 * const { toolRenderConfig, pluginRoutes, loading } = usePlugins();
 * ```
 */
export function usePlugins(): PluginContextValue {
  return useContext(PluginContext);
}
