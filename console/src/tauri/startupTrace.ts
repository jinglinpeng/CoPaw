import { isDesktopTauriRuntime } from "../utils/openExternalLink";

const MARK_PATH = "/api/desktop/startup-mark";

const reported = new Set<string>();

/**
 * Report a startup timing mark to the backend, which timestamps it into its own
 * log.
 *
 * The desktop UI boots across two page loads and shares no clock with the Tauri
 * shell or the Python sidecar, so the backend log is the only timeline all three
 * appear on. Inert outside the desktop runtime, and inert unless the backend runs
 * with QWENPAW_STARTUP_TRACE=1, where it answers 404 and this call is a no-op.
 *
 * Only the first occurrence of a name is sent, so callers on polling loops, in
 * effects that re-run, or in components that remount need no latch of their own.
 *
 * @param name Must match the backend's allowlist, or the mark is dropped.
 * @param apiBaseUrl Needed only from the Tauri gate page, which is not yet
 * same-origin with the backend.
 */
export function markStartup(name: string, apiBaseUrl = ""): void {
  if (reported.has(name) || !isDesktopTauriRuntime()) return;
  reported.add(name);
  const sincePageLoad = Math.round(performance.now() * 1000) / 1000;
  const url =
    `${apiBaseUrl}${MARK_PATH}?name=${encodeURIComponent(name)}` +
    `&since_page_load_ms=${sincePageLoad}`;
  // No body and no custom headers keeps the cross-origin call from the gate
  // page a CORS-simple request, so it needs no preflight. `keepalive` lets the
  // last mark survive the navigation that immediately follows it.
  void fetch(url, {
    method: "POST",
    cache: "no-store",
    keepalive: true,
  }).catch(() => {});
}
