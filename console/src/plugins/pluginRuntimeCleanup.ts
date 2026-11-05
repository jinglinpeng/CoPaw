import { pluginSystem } from "./hostExternals";
import { chatExtensions } from "./registry/chatExtensions";
import { menuRegistry, routeRegistry, slotRegistry } from "./registry/store";
import { memoryBackendRegistry } from "./memoryBackends";
import { settingsSearchRegistry } from "./settingsSearch";

// Successful entry fingerprints belong to the runtime and are invalidated by
// the same confirmed removal/update path as its registered capabilities.
export const loadedFrontendEntries = new Map<string, string>();

/** Remove every host-managed registration owned by one frontend plugin. */
export function removePluginRuntime(pluginId: string): void {
  loadedFrontendEntries.delete(pluginId);
  menuRegistry.removeBySource(pluginId);
  routeRegistry.removeBySource(pluginId);
  slotRegistry.removeBySource(pluginId);
  pluginSystem.removePlugin(pluginId);
  chatExtensions.disposeAll(pluginId);
  memoryBackendRegistry.removeBySource(pluginId);
  settingsSearchRegistry.removeBySource(pluginId);
}
