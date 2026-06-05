import { type ReactNode, useEffect, useRef } from "react";
import BackendLoadingPage from "./BackendLoadingPage";
import useBackendReadyPolling from "./useBackendReadyPolling";

interface Props {
  children: ReactNode;
  isDark?: boolean;
}

/**
 * Show the Tauri window once the first meaningful paint happens.
 * This pairs with `"visible": false` in tauri.conf.json to avoid
 * the white-flash on startup.
 */
async function showTauriWindow(): Promise<void> {
  try {
    const { getCurrentWindow } = await import("@tauri-apps/api/window");
    const win = getCurrentWindow();
    await win.show();
  } catch {
    // Not in Tauri or API unavailable — no-op
  }
}

export default function BackendReadyGate({ children, isDark }: Props) {
  const {
    shouldGate,
    status,
    elapsed,
    totalSec,
    errorMessage,
    readyUrl,
    retry,
  } = useBackendReadyPolling();

  const windowShownRef = useRef(false);

  // A3: Show the window after the first render of the loading page
  useEffect(() => {
    if (shouldGate && !windowShownRef.current) {
      windowShownRef.current = true;
      void showTauriWindow();
    }
  }, [shouldGate]);

  useEffect(() => {
    if (shouldGate && status === "ready" && readyUrl) {
      window.location.replace(readyUrl);
    }
  }, [readyUrl, shouldGate, status]);

  // Browser mode, or Tauri after it has navigated to the backend-hosted console.
  if (!shouldGate) {
    return <>{children}</>;
  }

  return (
    <BackendLoadingPage
      status={status}
      elapsed={elapsed}
      totalSec={totalSec}
      errorMessage={errorMessage}
      onRetry={retry}
      isDark={isDark}
    />
  );
}
