import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import { isDesktopApp } from "./backendRuntime";

export interface DesktopUpdateInfo {
  version: string;
  body?: string | null;
}

export interface UpdateProgress {
  downloaded: number;
  total: number | null;
}

export interface UpdateError {
  stage: "check" | "download" | "install";
  kind: "network" | "signature" | "other";
  message: string;
}

export async function checkDesktopUpdate(): Promise<DesktopUpdateInfo | null> {
  if (!isDesktopApp()) return null;
  return invoke<DesktopUpdateInfo | null>("check_desktop_update");
}

export async function installDesktopUpdate(): Promise<void> {
  if (!isDesktopApp()) return;
  await invoke<void>("install_desktop_update");
}

export interface UpdateEventHandlers {
  onCheckStart?: () => void;
  onDownloadProgress?: (progress: UpdateProgress) => void;
  onInstallStart?: () => void;
  onError?: (error: UpdateError) => void;
}

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
