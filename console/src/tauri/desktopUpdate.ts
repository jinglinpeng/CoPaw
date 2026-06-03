import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import { isDesktopApp } from "./backendRuntime";

export interface DesktopUpdateInfo {
  version: string;
  body?: string | null;
}

export type UpdateStage = "check" | "download" | "install";
export type UpdateErrorKind = "network" | "signature" | "other";

export interface UpdateProgress {
  downloaded: number;
  total: number | null;
}

export interface UpdateError {
  stage: UpdateStage;
  kind: UpdateErrorKind;
  message: string;
}

export async function checkDesktopUpdate(): Promise<DesktopUpdateInfo | null> {
  if (!isDesktopApp()) return null;
  return invoke<DesktopUpdateInfo | null>("check_desktop_update");
}

/**
 * Kick off the install flow. Resolves immediately; progress and outcome are
 * delivered through `update:*` events (see {@link onUpdateEvent}).
 */
export async function installDesktopUpdate(): Promise<void> {
  if (!isDesktopApp()) return;
  await invoke<void>("install_desktop_update");
}

export interface UpdateEventHandlers {
  onCheckStart?: () => void;
  onDownloadProgress?: (progress: UpdateProgress) => void;
  onInstallStart?: () => void;
  onInstallDone?: () => void;
  onError?: (error: UpdateError) => void;
}

/** Subscribe to all `update:*` events. Returns a function that unsubscribes. */
export async function onUpdateEvent(
  handlers: UpdateEventHandlers,
): Promise<UnlistenFn> {
  const unlisteners: UnlistenFn[] = [];

  if (handlers.onCheckStart) {
    unlisteners.push(
      await listen<unknown>(
        "update:check-start",
        () => handlers.onCheckStart?.(),
      ),
    );
  }
  if (handlers.onDownloadProgress) {
    unlisteners.push(
      await listen<UpdateProgress>(
        "update:download-progress",
        (event) => handlers.onDownloadProgress?.(event.payload),
      ),
    );
  }
  if (handlers.onInstallStart) {
    unlisteners.push(
      await listen<unknown>(
        "update:install-start",
        () => handlers.onInstallStart?.(),
      ),
    );
  }
  if (handlers.onInstallDone) {
    unlisteners.push(
      await listen<unknown>(
        "update:install-done",
        () => handlers.onInstallDone?.(),
      ),
    );
  }
  if (handlers.onError) {
    unlisteners.push(
      await listen<UpdateError>(
        "update:error",
        (event) => handlers.onError?.(event.payload),
      ),
    );
  }

  return () => {
    unlisteners.forEach((u) => u());
  };
}
