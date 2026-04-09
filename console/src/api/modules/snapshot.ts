import { request } from "../request";
import { getApiUrl } from "../config";
import { buildAuthHeaders } from "../authHeaders";
import type {
  CreateSnapshotRequest,
  ImportResult,
  ImportSnapshotParams,
  RestoreSnapshotRequest,
  SnapshotInfo,
} from "../types/snapshot";

function formatErrorDetail(detail: unknown): string | null {
  if (detail == null) return null;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const parts = detail.map((item) => {
      if (typeof item === "string") return item;
      if (item && typeof item === "object" && "msg" in item) {
        return String((item as { msg?: string }).msg ?? "");
      }
      return "";
    });
    const joined = parts.filter(Boolean).join("; ");
    return joined || null;
  }
  if (typeof detail === "object" && "message" in detail) {
    return String((detail as { message?: unknown }).message ?? "");
  }
  return null;
}

async function readErrorMessage(response: Response): Promise<string> {
  const text = await response.text().catch(() => "");
  if (!text) {
    return `Request failed: ${response.status} ${response.statusText}`;
  }

  try {
    const payload = JSON.parse(text) as { detail?: unknown; message?: string };
    const fromDetail = formatErrorDetail(payload.detail);
    return (
      fromDetail ||
      payload.message ||
      `Request failed: ${response.status} ${response.statusText}`
    );
  } catch {
    return text;
  }
}

function parseContentDispositionFilename(
  disposition: string | null,
  fallback: string,
): string {
  if (!disposition) return fallback;
  const m = disposition.match(/filename="(.+?)"/);
  return m?.[1] || fallback;
}

export const snapshotApi = {
  listSnapshots: () => request<SnapshotInfo[]>("/snapshots"),

  getSnapshot: (snapshotId: string) =>
    request<SnapshotInfo>(`/snapshots/${snapshotId}`),

  createSnapshot: (payload: CreateSnapshotRequest) =>
    request<SnapshotInfo>("/snapshots", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  deleteSnapshot: (snapshotId: string) =>
    request<{ success: boolean; snapshot_id: string }>(
      `/snapshots/${snapshotId}`,
      {
        method: "DELETE",
      },
    ),

  restoreSnapshot: (
    snapshotId: string,
    payload: RestoreSnapshotRequest,
  ): Promise<{ success: boolean; agent_id: string; mode: string; message: string }> =>
    request(`/snapshots/${snapshotId}/restore`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  exportSnapshot: async (
    snapshotId: string,
    includeSecrets = false,
    password?: string,
  ): Promise<{ blob: Blob; filename: string }> => {
    const params = new URLSearchParams();
    if (includeSecrets) {
      params.set("include_secrets", "true");
    }
    const pw = password?.trim();
    if (pw) {
      params.set("password", pw);
    }
    const qs = params.toString();
    const query = qs ? `?${qs}` : "";
    const response = await fetch(
      getApiUrl(`/snapshots/${snapshotId}/export${query}`),
      {
        method: "GET",
        headers: buildAuthHeaders(),
      },
    );
    if (!response.ok) {
      throw new Error(await readErrorMessage(response));
    }

    const blob = await response.blob();
    const filename = parseContentDispositionFilename(
      response.headers.get("Content-Disposition"),
      `snapshot-${snapshotId}.zip`,
    );
    return { blob, filename };
  },

  importSnapshot: async (
    file: File,
    params?: ImportSnapshotParams,
  ): Promise<ImportResult> => {
    const formData = new FormData();
    formData.append("file", file);

    const search = new URLSearchParams();
    const agentId = params?.agentId;
    if (agentId?.trim()) {
      search.set("agent_id", agentId.trim());
    }
    if (params?.force) {
      search.set("force", "true");
    }
    const mapping = params?.agentMapping;
    if (mapping !== undefined) {
      const json = JSON.stringify(mapping);
      formData.append("agent_mappings", json);
    }
    const importPassword = params?.password?.trim();
    if (importPassword) {
      formData.append("password", importPassword);
    }

    const suffix = search.toString() ? `?${search.toString()}` : "";
    const response = await fetch(getApiUrl(`/snapshots/import${suffix}`), {
      method: "POST",
      headers: buildAuthHeaders(),
      body: formData,
    });
    if (!response.ok) {
      throw new Error(await readErrorMessage(response));
    }
    return (await response.json()) as ImportResult;
  },
};

