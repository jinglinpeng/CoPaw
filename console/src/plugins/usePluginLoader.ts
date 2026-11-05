/** Frontend plugin loading utilities. */

import { getApiToken, getApiUrl } from "../api/config";
import {
  loadedFrontendEntries,
  removePluginRuntime,
} from "./pluginRuntimeCleanup";
import { routeRegistry } from "./registry/store";

interface FrontendPluginInfo {
  id: string;
  name: string;
  plugin_type?: string;
  frontend_entry?: string;
  version?: string;
}

export interface PluginLoadSummary {
  loaded: number;
  failed: string[];
  retryable?: boolean;
}

const loadingPlugins = new Map<string, Promise<void>>();

function authHeaders(): Record<string, string> {
  const token = getApiToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function resolveUrl(pluginId: string, apiPath: string): string {
  return getApiUrl(`frontend_plugin/${pluginId}/files/${apiPath}`);
}

/** Only transport failures and the explicit startup contract retry automatically. */
export class PluginInitializationError extends Error {
  readonly retryable: boolean;
  readonly waiting: boolean;
  constructor(message: string, retryable = false, waiting = false) {
    super(message);
    this.retryable = retryable;
    this.waiting = waiting;
  }
}

function runtimeScope(): string {
  return `${getApiUrl("")}\0${getApiToken()}`;
}
let loadedScope = "";

function synchronizeScope(): string {
  const scope = runtimeScope();
  if (loadedScope !== scope) {
    for (const id of loadedFrontendEntries.keys()) removePluginRuntime(id);
    loadedScope = scope;
  }
  return scope;
}

function checkActive(scope: string, signal?: AbortSignal): void {
  signal?.throwIfAborted();
  if (scope !== runtimeScope())
    throw new DOMException("Plugin runtime changed", "AbortError");
}

async function fetchPluginResource(
  url: string,
  signal?: AbortSignal,
): Promise<Response> {
  try {
    return await fetch(url, { headers: authHeaders(), signal });
  } catch (error) {
    signal?.throwIfAborted();
    // Keep plugin-code TypeErrors distinct from fetch transport errors.
    throw new PluginInitializationError(
      error instanceof Error ? error.message : "Plugin connection failed",
      true,
    );
  }
}

async function readPluginBody(
  response: Response,
  signal?: AbortSignal,
): Promise<string> {
  try {
    return await response.text();
  } catch (error) {
    signal?.throwIfAborted();
    throw new PluginInitializationError(
      error instanceof Error ? error.message : "Plugin response interrupted",
      true,
    );
  }
}

async function fetchFrontendPlugins(
  signal?: AbortSignal,
): Promise<FrontendPluginInfo[]> {
  const response = await fetchPluginResource(
    getApiUrl("/frontend_plugin"),
    signal,
  );
  const body = await readPluginBody(response, signal);
  if (!response.ok) {
    let payload;
    try {
      payload = JSON.parse(body);
    } catch {
      payload = null;
    }
    const waiting =
      response.status === 503 &&
      payload?.detail?.code === "FRONTEND_PLUGINS_STARTING";
    throw new PluginInitializationError(
      payload?.detail?.message ||
        `Failed to list frontend plugins (${response.status})`,
      waiting,
      waiting,
    );
  }
  return JSON.parse(body);
}

async function executePluginScript(
  entryUrl: string,
  scope: string,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetchPluginResource(entryUrl, signal);
  if (!response.ok) throw new Error(`HTTP ${response.status} for ${entryUrl}`);
  const script = await readPluginBody(response, signal);
  checkActive(scope, signal);
  const blobUrl = URL.createObjectURL(
    new Blob([script], { type: "application/javascript" }),
  );
  try {
    // Native import cannot be interrupted once evaluation starts. Do not start
    // it after cancellation, and retain success if cancellation occurs later.
    await import(/* @vite-ignore */ blobUrl);
  } finally {
    URL.revokeObjectURL(blobUrl);
  }
}

async function executeRegisteredPlugin(
  plugin: FrontendPluginInfo,
  scope: string,
  options: LoadPluginOptions,
): Promise<void> {
  checkActive(scope, options.signal);
  const key = `${scope}\0${plugin.version ?? ""}\0${plugin.frontend_entry}`;
  if (!options.force && loadedFrontendEntries.get(plugin.id) === key) return;
  if (options.force || loadedFrontendEntries.has(plugin.id))
    removePluginRuntime(plugin.id);
  try {
    await executePluginScript(
      resolveUrl(plugin.id, plugin.frontend_entry!),
      scope,
      options.signal,
    );
    if (scope !== runtimeScope())
      throw new DOMException("Plugin runtime changed", "AbortError");
    loadedFrontendEntries.set(plugin.id, key);
  } catch (error) {
    removePluginRuntime(plugin.id);
    throw error;
  }
}

// A reconciliation may share an install/update already in flight. Cancel this
// caller's wait without cancelling work owned by the other caller.
function awaitPendingPlugin(pending: Promise<void>, signal?: AbortSignal) {
  if (!signal) return pending;
  signal.throwIfAborted();
  return new Promise<void>((resolve, reject) => {
    const abort = () => reject(signal.reason);
    signal.addEventListener("abort", abort, { once: true });
    pending.then(
      () => {
        signal.removeEventListener("abort", abort);
        resolve();
      },
      (error) => {
        signal.removeEventListener("abort", abort);
        reject(error);
      },
    );
  });
}

async function loadStartupPlugin(
  plugin: FrontendPluginInfo,
  scope: string,
  signal?: AbortSignal,
): Promise<void> {
  const pending = loadingPlugins.get(plugin.id);
  if (pending) {
    try {
      await awaitPendingPlugin(pending, signal);
    } catch (error) {
      signal?.throwIfAborted();
      if (!(error instanceof DOMException && error.name === "AbortError"))
        throw error;
    }
    checkActive(scope, signal);
  }
  const key = `${scope}\0${plugin.version ?? ""}\0${plugin.frontend_entry}`;
  if (loadedFrontendEntries.get(plugin.id) === key) return;
  const promise = executeRegisteredPlugin(plugin, scope, { signal }).finally(
    () => {
      if (loadingPlugins.get(plugin.id) === promise)
        loadingPlugins.delete(plugin.id);
    },
  );
  loadingPlugins.set(plugin.id, promise);
  return promise;
}

/** One inventory reconciliation; PluginProvider owns its cancellable lifetime. */
export async function loadAllPlugins(
  signal?: AbortSignal,
): Promise<PluginLoadSummary> {
  const scope = synchronizeScope();
  checkActive(scope, signal);
  const plugins = await fetchFrontendPlugins(signal);
  checkActive(scope, signal);
  const loadable = plugins.filter((plugin) => plugin.frontend_entry);
  const installed = new Set(loadable.map((plugin) => plugin.id));
  for (const id of loadedFrontendEntries.keys()) {
    if (!installed.has(id)) removePluginRuntime(id);
  }
  const results = await Promise.allSettled(
    loadable.map((plugin) => loadStartupPlugin(plugin, scope, signal)),
  );
  signal?.throwIfAborted();
  const failed = results.flatMap((result, index) =>
    result.status === "rejected"
      ? [`${loadable[index].id}: ${result.reason}`]
      : [],
  );
  // A permanent entry error needs user action. Do not repeatedly execute a
  // broken plugin just because another entry also had a transport failure.
  const failures = results.filter((result) => result.status === "rejected");
  const retryable =
    failures.length > 0 &&
    failures.every(
      (result) =>
        result.reason instanceof PluginInitializationError &&
        result.reason.retryable,
    );
  return {
    loaded: loadable.length - failed.length,
    failed,
    ...(retryable ? { retryable: true } : {}),
  };
}

interface LoadPluginOptions {
  force?: boolean;
  signal?: AbortSignal;
  expectedType?: "app";
  entryPage?: string;
}

function loadFrontendPlugin(
  pluginId: string,
  options: LoadPluginOptions = {},
): Promise<void> {
  const scope = synchronizeScope();
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
    const plugins = await fetchFrontendPlugins(options.signal);
    checkActive(scope, options.signal);
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
    try {
      await executeRegisteredPlugin(plugin, scope, options);
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
  loadedFrontendEntries.clear();
  loadedScope = "";
}
