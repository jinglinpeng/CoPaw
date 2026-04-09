export type SnapshotScope = "single" | "selected" | "all";
export type RestoreMode = "in_place" | "clone";
export type AgentStatus = "ready" | "needs_review" | "needs_setup";

export interface SnapshotInfo {
  snapshot_id: string;
  filename: string;
  scope: SnapshotScope;
  agent_ids: string[];
  created_at: string;
  size_bytes: number;
  includes_secrets: boolean;
  includes_global: boolean;
  notes: string;
}

export interface CreateSnapshotRequest {
  scope: SnapshotScope;
  agent_ids?: string[];
  include_secrets?: boolean;
  include_global?: boolean;
  exclude_sessions?: boolean;
  exclude_memory?: boolean;
  note?: string;
}

export interface RestoreSnapshotRequest {
  agent_id: string;
  mode?: RestoreMode;
  new_agent_id?: string;
}

export interface TodoItem {
  severity: "required" | "suggested" | string;
  message: string;
  action?: string;
}

/** Per-agent line item when backend returns multi-import breakdown */
export interface ImportAgentResultItem {
  source_agent_id?: string;
  target_agent_id?: string;
  source?: string;
  target?: string;
  status?: AgentStatus | string;
  message?: string;
}

export interface ImportSingleResult {
  agent_id: string;
  status: AgentStatus;
  file_summary?: Record<string, string>;
  todos?: TodoItem[];
  /** Optional: one row per source→target import */
  agent_results?: ImportAgentResultItem[];
  /** Optional: backend native field name for per-agent outcomes */
  agent_outcomes?: ImportAgentResultItem[];
}

export interface ImportBatchResult {
  /** Preferred field for batch results */
  results?: ImportSingleResult[];
  /** Backward/parallel compatible field name */
  batch_results?: ImportSingleResult[];
}

export type ImportResult = ImportSingleResult | ImportBatchResult | ImportSingleResult[];

export interface ImportSnapshotParams {
  agentId?: string;
  force?: boolean;
  /** Snapshot package source agent id → workspace agent id to create/overwrite */
  agentMapping?: Record<string, string>;
  /** Optional passphrase for encrypted packages */
  password?: string;
}
