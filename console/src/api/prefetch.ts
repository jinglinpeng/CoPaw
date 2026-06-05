/**
 * prefetch.ts — consume prefetched API data from the inline <script> in index.html.
 *
 * The inline script fires requests for critical APIs before the main JS bundle
 * has finished parsing. This module provides a typed interface to consume those
 * results, with automatic fallback if prefetch is unavailable or failed.
 */

interface PrefetchStore {
  authStatus?: Promise<unknown>;
  providers?: Promise<unknown>;
  activeModels?: Promise<unknown>;
  codingMode?: Promise<unknown>;
}

declare global {
  interface Window {
    __PREFETCH__?: PrefetchStore;
  }
}

/**
 * Consume a prefetched promise by key. Returns the promise if it exists,
 * then clears it so subsequent calls get `undefined` (forcing normal fetch).
 * This ensures each prefetch result is consumed exactly once.
 */
export function consumePrefetch<T>(key: keyof PrefetchStore): Promise<T> | undefined {
  const store = window.__PREFETCH__;
  if (!store) return undefined;
  const promise = store[key] as Promise<T> | undefined;
  if (promise) {
    // Clear after consumption — prevents stale data on subsequent calls
    delete store[key];
  }
  return promise;
}
