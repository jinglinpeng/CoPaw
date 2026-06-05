import { request } from "../request";
import { consumePrefetch } from "../prefetch";

export interface CodingModeState {
  enabled: boolean;
  project_dir: string | null;
  agent_id: string;
}

export interface CodingModeToggleResponse {
  enabled: boolean;
  agent_id: string;
}

export const codingModeApi = {
  /** Read Coding Mode state (enabled + project_dir) from agent.json. */
  get: () => {
    // Try to consume the prefetched result from the inline script in index.html
    const prefetched = consumePrefetch<CodingModeState>("codingMode");
    if (prefetched) {
      return prefetched.catch(() => request<CodingModeState>("/coding-mode"));
    }
    return request<CodingModeState>("/coding-mode");
  },

  /** Enable or disable Coding Mode; backend reloads the agent. */
  toggle: (enabled: boolean) =>
    request<CodingModeToggleResponse>("/coding-mode", {
      method: "POST",
      body: JSON.stringify({ enabled }),
    }),
};
