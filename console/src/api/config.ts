// VITE_API_BASE_URL and TOKEN are declared globally in src/vite-env.d.ts.

const AUTH_TOKEN_KEY = "qwenpaw_auth_token";

declare global {
  interface Window {
    __TAURI__?: {
      core?: {
        invoke?: <T>(
          command: string,
          args?: Record<string, unknown>,
        ) => Promise<T>;
      };
    };
  }
}

let runtimeApiBaseUrl = "";

export function getApiBaseUrl(): string {
  return (
    runtimeApiBaseUrl ||
    (typeof VITE_API_BASE_URL !== "undefined" ? VITE_API_BASE_URL : "")
  );
}

export function isTauriRuntime(): boolean {
  return typeof window !== "undefined" && !!window.__TAURI__?.core?.invoke;
}

export async function initRuntimeApiBaseUrl(): Promise<string> {
  const baseUrl = getApiBaseUrl();
  const invoke =
    typeof window !== "undefined" ? window.__TAURI__?.core?.invoke : undefined;
  if (baseUrl || !invoke) return baseUrl;

  const port = await invoke<number>("backend_port");
  runtimeApiBaseUrl = `http://127.0.0.1:${port}`;

  // Propagate the resolved URL into the already-installed host externals so
  // plugin bundles that cached window.QwenPaw.host.apiBaseUrl at startup get
  // the correct address.
  const qwenPaw = window.QwenPaw;
  if (qwenPaw?.host) qwenPaw.host.apiBaseUrl = runtimeApiBaseUrl;

  return runtimeApiBaseUrl;
}

/**
 * Get the full API URL with /api prefix
 * @param path - API path (e.g., "/models", "/skills")
 * @returns Full API URL (e.g., "http://localhost:8088/api/models" or "/api/models")
 */
export function getApiUrl(path: string): string {
  const base = getApiBaseUrl();
  const apiPrefix = "/api";
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  return `${base}${apiPrefix}${normalizedPath}`;
}

/**
 * Get the API token - checks localStorage first (auth login),
 * then falls back to the build-time TOKEN constant.
 * @returns API token string or empty string
 */
export function getApiToken(): string {
  const stored = localStorage.getItem(AUTH_TOKEN_KEY);
  if (stored) return stored;
  return typeof TOKEN !== "undefined" ? TOKEN : "";
}

/**
 * Store the auth token in localStorage after login.
 */
export function setAuthToken(token: string): void {
  localStorage.setItem(AUTH_TOKEN_KEY, token);
}

/**
 * Remove the auth token from localStorage (logout / 401).
 */
export function clearAuthToken(): void {
  localStorage.removeItem(AUTH_TOKEN_KEY);
}
