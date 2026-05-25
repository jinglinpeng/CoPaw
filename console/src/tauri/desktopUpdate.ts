import { invoke } from "@tauri-apps/api/core";
import { isTauriRuntime } from "./backendRuntime";

export interface DesktopUpdateInfo {
  version: string;
  body?: string | null;
}

export async function checkDesktopUpdate(): Promise<DesktopUpdateInfo | null> {
  if (!isTauriRuntime()) return null;
  return invoke<DesktopUpdateInfo | null>("check_desktop_update");
}

export async function installDesktopUpdate(): Promise<void> {
  if (!isTauriRuntime()) return;
  await invoke<void>("install_desktop_update");
}
