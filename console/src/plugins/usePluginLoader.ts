/** Frontend plugin loading utilities. */

import { getApiToken, getApiUrl } from "../api/config";
import { removePluginRuntime } from "./pluginRuntimeCleanup";
import { routeRegistry } from "./registry/store";

interface FrontendPluginInfo {
  id: string;
  name: string;
  plugin_type?: string;
  frontend_entry?: string;
}

export interface PluginLoadSummary {
  loaded: number;
  failed: string[];
}

const loadingPlugins = new Map<string, Promise<void>>();

function authHeaders(): Record<string, string> {
  const token = getApiToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function resolveUrl(pluginId: string, apiPath: string): string {
  return getApiUrl(`frontend_plugin/${pluginId}/files/${apiPath}`);
}

// Retry only the initial list request, before any plugin code can register.
const STARTUP_RETRY_DELAYS_MS = [1000, 2000, 4000, 8000];

async function fetchFrontendPlugins(
  retryDelays: number[] = [],
): Promise<FrontendPluginInfo[]> {
  for (let attempt = 0; ; attempt++) {
    let retryable = false;
    try {
      const response = await fetch(getApiUrl("/frontend_plugin"), {
        headers: authHeaders(),
      });
      retryable = response.status === 404 || response.status >= 500;
      if (!response.ok) {
        throw new Error(`Failed to list frontend plugins (${response.status})`);
      }
      return await response.json();
    } catch (error) {
      if (
        attempt >= retryDelays.length ||
        (!retryable && !(error instanceof TypeError))
      ) {
        throw error;
      }
      await new Promise((resolve) => setTimeout(resolve, retryDelays[attempt]));
    }
  }
}

async function executePluginScript(entryUrl: string): Promise<void> {
  const response = await fetch(entryUrl, { headers: authHeaders() });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status} for ${entryUrl}`);
  }

  const blobUrl = URL.createObjectURL(
    new Blob([await response.text()], { type: "application/javascript" }),
  );
  try {
    await import(/* @vite-ignore */ blobUrl);
  } finally {
    URL.revokeObjectURL(blobUrl);
  }
}

/** Load every installed frontend plugin during Console startup. */
export async function loadAllPlugins(): Promise<PluginLoadSummary> {
  const plugins = await fetchFrontendPlugins(STARTUP_RETRY_DELAYS_MS);

  const loadable = plugins.filter((plugin) => plugin.frontend_entry);
  const results = await Promise.allSettled(
    loadable.map((plugin) =>
      executePluginScript(resolveUrl(plugin.id, plugin.frontend_entry!)),
    ),
  );
  const failed = results.flatMap((result, index) =>
    result.status === "rejected"
      ? [`${loadable[index].id}: ${result.reason}`]
      : [],
  );
  return { loaded: loadable.length - failed.length, failed };
}

interface LoadPluginOptions {
  force?: boolean;
  expectedType?: "app";
  entryPage?: string;
}

function loadFrontendPlugin(
  pluginId: string,
  options: LoadPluginOptions = {},
): Promise<void> {
  const registered = () =>
    routeRegistry
      .snapshot()
      .some(
        (route) =>
          route.source === pluginId &&
          route.path.startsWith("/apps/") &&
          (!options.entryPage || route.path === options.entryPage),
      );

  if (!options.force && options.expectedType === "app" && registered()) {
    return Promise.resolve();
  }

  const promise = (async () => {
    const plugins = await fetchFrontendPlugins();
    const plugin = plugins.find((item) => item.id === pluginId);
    if (!plugin?.frontend_entry) {
      if (options.expectedType === "app") {
        throw new Error(`PawApp frontend plugin not found: ${pluginId}`);
      }
      return;
    }
    if (options.expectedType && plugin.plugin_type !== options.expectedType) {
      throw new Error(`PawApp frontend plugin not found: ${pluginId}`);
    }
    if (options.force) removePluginRuntime(pluginId);
    try {
      await executePluginScript(resolveUrl(plugin.id, plugin.frontend_entry));
      if (options.expectedType === "app" && !registered()) {
        throw new Error(`PawApp ${pluginId} did not register its app route`);
      }
    } catch (error) {
      removePluginRuntime(pluginId);
      throw error;
    }
  })().finally(() => {
    loadingPlugins.delete(pluginId);
  });

  loadingPlugins.set(pluginId, promise);
  return promise;
}

/** Load one newly installed PawApp without reloading the page. */
export function loadPawApp(
  appId: string,
  entryPage?: string,
  options: { force?: boolean } = {},
): Promise<void> {
  if (options.force) return reloadPawApp(appId, entryPage);
  const pending = loadingPlugins.get(appId);
  if (pending) return pending;
  return loadFrontendPlugin(appId, {
    expectedType: "app",
    entryPage,
    force: options.force,
  });
}

/** Force reload an installed PawApp after an update. */
export function reloadPawApp(appId: string, entryPage?: string): Promise<void> {
  const pending = loadingPlugins.get(appId);
  if (pending) {
    return pending.then(() =>
      loadFrontendPlugin(appId, {
        expectedType: "app",
        entryPage,
        force: true,
      }),
    );
  }
  return loadFrontendPlugin(appId, {
    expectedType: "app",
    entryPage,
    force: true,
  });
}

/** Reload a frontend plugin after installation or update. */
export function reloadFrontendPlugin(pluginId: string): Promise<boolean> {
  const pending = loadingPlugins.get(pluginId);
  if (pending) {
    return pending.then(() =>
      loadFrontendPlugin(pluginId, { force: true }).then(() => true),
    );
  }
  return loadFrontendPlugin(pluginId, { force: true }).then(() => true);
}

/** Reset pending loads between unit tests. */
export function resetPawAppLoaderForTests(): void {
  loadingPlugins.clear();
}
