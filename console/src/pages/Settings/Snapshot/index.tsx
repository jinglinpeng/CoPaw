import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Form,
  Input,
  Modal,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Typography,
  Upload,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  DeleteOutlined,
  DownloadOutlined,
  ImportOutlined,
  MinusCircleOutlined,
  PlusOutlined,
  RollbackOutlined,
  UploadOutlined,
} from "@ant-design/icons";
import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";

import api from "../../../api";
import { agentsApi } from "../../../api/modules/agents";
import type {
  AgentStatus,
  CreateSnapshotRequest,
  ImportAgentResultItem,
  ImportResult,
  ImportSingleResult,
  RestoreSnapshotRequest,
  SnapshotInfo,
  SnapshotScope,
} from "../../../api/types/snapshot";
import { useAppMessage } from "../../../hooks/useAppMessage";
import { useAgentStore } from "../../../stores/agentStore";
import { PageHeader } from "@/components/PageHeader";
import styles from "./index.module.less";

interface SnapshotRow extends SnapshotInfo {
  key: string;
}

interface RestoreModalState {
  open: boolean;
  snapshot: SnapshotInfo | null;
}

function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let idx = 0;
  while (value >= 1024 && idx < units.length - 1) {
    value /= 1024;
    idx += 1;
  }
  return `${value.toFixed(value >= 10 || idx === 0 ? 0 : 1)} ${units[idx]}`;
}

function formatSnapshotStatus(status: AgentStatus, t: TFunction) {
  if (status === "needs_setup") return t("snapshot.statusNeedsSetup");
  if (status === "needs_review") return t("snapshot.statusNeedsReview");
  return t("snapshot.statusReady");
}

function formatSnapshotStatusLoose(status: string | undefined, t: TFunction) {
  if (status === "needs_setup" || status === "needs_review" || status === "ready") {
    return formatSnapshotStatus(status, t);
  }
  return status && status.length > 0 ? status : "—";
}

interface ImportFormValues {
  agent_id?: string;
  force?: boolean;
  password?: string;
  mapping_rows?: Array<{ source?: string; target?: string }>;
}

interface ExportDialogState {
  open: boolean;
  snapshot: SnapshotInfo | null;
}

function importAgentResultKey(row: ImportAgentResultItem, idx?: number): string {
  const s = row.source_agent_id ?? row.source ?? "";
  const t = row.target_agent_id ?? row.target ?? "";
  return `${s}->${t}-${idx ?? 0}`;
}

function importAgentResultSourceTarget(row: ImportAgentResultItem): {
  source: string;
  target: string;
} {
  return {
    source: row.source_agent_id ?? row.source ?? "—",
    target: row.target_agent_id ?? row.target ?? "—",
  };
}

function buildAgentMappingFromRows(
  rows: ImportFormValues["mapping_rows"],
): Record<string, string> | undefined {
  if (!rows || rows.length === 0) return undefined;
  const entries = rows
    .map((row) => {
      const source = row.source?.trim();
      const target = row.target?.trim();
      if (!source || !target) return null;
      return [source, target] as const;
    })
    .filter((entry): entry is readonly [string, string] => entry !== null);
  if (entries.length === 0) return undefined;
  return Object.fromEntries(entries);
}

function normalizeImportResult(result: ImportResult | null): ImportSingleResult[] {
  if (!result) return [];
  if (Array.isArray(result)) return result;
  if ("results" in result && Array.isArray(result.results)) return result.results;
  if ("batch_results" in result && Array.isArray(result.batch_results)) return result.batch_results;
  return [result];
}

export default function SnapshotPage() {
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const selectedAgent = useAgentStore((s) => s.selectedAgent);

  const [loading, setLoading] = useState<boolean>(true);
  const [creating, setCreating] = useState<boolean>(false);
  const [importing, setImporting] = useState<boolean>(false);
  const [restoring, setRestoring] = useState<boolean>(false);
  const [downloadingId, setDownloadingId] = useState<string>("");
  const [deletingId, setDeletingId] = useState<string>("");

  const [rows, setRows] = useState<SnapshotRow[]>([]);
  const [agentOptions, setAgentOptions] = useState<string[]>([selectedAgent || "default"]);

  const [createOpen, setCreateOpen] = useState<boolean>(false);
  const [importOpen, setImportOpen] = useState<boolean>(false);
  const [restoreModal, setRestoreModal] = useState<RestoreModalState>({
    open: false,
    snapshot: null,
  });

  const [createForm] = Form.useForm<CreateSnapshotRequest>();
  const [restoreForm] = Form.useForm<RestoreSnapshotRequest>();
  const [importForm] = Form.useForm<ImportFormValues>();
  const [importFile, setImportFile] = useState<File | null>(null);
  const [importResult, setImportResult] = useState<ImportResult | null>(null);
  const [exportDialog, setExportDialog] = useState<ExportDialogState>({
    open: false,
    snapshot: null,
  });
  const [exportPassword, setExportPassword] = useState<string>("");

  const loadAgents = useCallback(async () => {
    try {
      const data = await agentsApi.listAgents();
      const ids = data.agents.map((a) => a.id);
      if (ids.length > 0) {
        setAgentOptions(ids);
      }
    } catch (err) {
      // Non-blocking, snapshot page can still work.
      console.warn("Failed to load agent ids for snapshot:", err);
    }
  }, []);

  const loadSnapshots = useCallback(async () => {
    setLoading(true);
    try {
      const snapshots = await api.listSnapshots();
      setRows(
        snapshots.map((s) => ({
          ...s,
          key: s.snapshot_id,
        })),
      );
    } catch (err) {
      console.error("Failed to load snapshots:", err);
      message.error(t("snapshot.loadFailed"));
      setRows([]);
    } finally {
      setLoading(false);
    }
  }, [message, t]);

  useEffect(() => {
    void loadAgents();
    void loadSnapshots();
  }, [loadAgents, loadSnapshots]);

  const scope = Form.useWatch("scope", createForm) as SnapshotScope | undefined;
  const normalizedImportResults = useMemo(
    () => normalizeImportResult(importResult),
    [importResult],
  );
  const primaryImportResult = normalizedImportResults[0];
  const importAgentRows =
    primaryImportResult?.agent_outcomes ?? primaryImportResult?.agent_results ?? [];

  const columns: ColumnsType<SnapshotRow> = useMemo(
    () => [
      {
        title: t("snapshot.note"),
        dataIndex: "notes",
        key: "notes",
        render: (_, row) => (
          <div className={styles.noteCol}>
            <div className={styles.noteTitle}>{row.notes || t("snapshot.noNote")}</div>
            <div className={styles.noteSub}>{row.snapshot_id}</div>
          </div>
        ),
      },
      {
        title: t("snapshot.agents"),
        dataIndex: "agent_ids",
        key: "agent_ids",
        width: 240,
        render: (ids: string[]) => {
          if (!ids || ids.length === 0) return "-";
          const visible = ids.slice(0, 2);
          const extra = ids.length - visible.length;
          return (
            <Space size={4} wrap>
              {visible.map((id) => (
                <Tag key={id}>{id}</Tag>
              ))}
              {extra > 0 ? <Tag>+{extra}</Tag> : null}
            </Space>
          );
        },
      },
      {
        title: t("snapshot.createdAt"),
        dataIndex: "created_at",
        key: "created_at",
        width: 180,
        render: (v: string) => new Date(v).toLocaleString(),
      },
      {
        title: t("snapshot.size", "大小"),
        dataIndex: "size_bytes",
        key: "size_bytes",
        width: 100,
        render: (v: number) => formatBytes(v),
      },
      {
        title: t("snapshot.content", "内容"),
        key: "content",
        width: 180,
        render: (_, row) => (
          <Space size={4} wrap>
            <Tag color="blue">{t("snapshot.tagWorkspace", "工作区")}</Tag>
            {row.includes_global ? <Tag>{t("snapshot.tagGlobal", "全局")}</Tag> : null}
            {row.includes_secrets ? (
              <Tag color="orange">{t("snapshot.tagSecrets", "密钥")}</Tag>
            ) : null}
          </Space>
        ),
      },
      {
        title: t("snapshot.actions", "操作"),
        key: "actions",
        width: 260,
        render: (_, row) => (
          <Space size={8}>
            <Button
              size="small"
              icon={<RollbackOutlined />}
              onClick={() => {
                setRestoreModal({ open: true, snapshot: row });
                restoreForm.setFieldsValue({
                  agent_id: row.agent_ids?.[0] || selectedAgent || "default",
                  mode: "in_place",
                });
              }}
            >
              {t("snapshot.restore", "恢复")}
            </Button>
            <Button
              size="small"
              icon={<DownloadOutlined />}
              loading={downloadingId === row.snapshot_id}
              onClick={() => {
                setExportPassword("");
                setExportDialog({ open: true, snapshot: row });
              }}
            >
              {t("snapshot.export", "导出")}
            </Button>
            <Button
              danger
              size="small"
              icon={<DeleteOutlined />}
              loading={deletingId === row.snapshot_id}
              onClick={() => {
                Modal.confirm({
                  title: t("snapshot.deleteConfirmTitle", "删除快照"),
                  content: t(
                    "snapshot.deleteConfirmContent",
                    "确认删除该快照？此操作不可恢复。",
                  ),
                  okText: t("common.delete", "删除"),
                  cancelText: t("common.cancel", "取消"),
                  okButtonProps: { danger: true },
                  onOk: async () => {
                    try {
                      setDeletingId(row.snapshot_id);
                      await api.deleteSnapshot(row.snapshot_id);
                      message.success(t("snapshot.deleteSuccess", "删除成功"));
                      await loadSnapshots();
                    } catch (err: unknown) {
                      const msg = err instanceof Error ? err.message : t("snapshot.deleteFailed", "删除失败");
                      message.error(msg);
                    } finally {
                      setDeletingId("");
                    }
                  },
                });
              }}
            >
              {t("common.delete", "删除")}
            </Button>
          </Space>
        ),
      },
    ],
    [
      t,
      restoreForm,
      selectedAgent,
      downloadingId,
      deletingId,
      message,
      loadSnapshots,
    ],
  );

  return (
    <div className={styles.snapshotPage}>
      <PageHeader
        parent={t("nav.settings", "设置")}
        current={t("snapshot.title", "快照")}
        subRow={
          <Typography.Text type="secondary">
            {t(
              "snapshot.subtitle",
              "管理所有 Agent 的快照。与当前选中的 Agent 无关。",
            )}
          </Typography.Text>
        }
        extra={
          <Space>
            <Button
              icon={<ImportOutlined />}
              onClick={() => {
                importForm.resetFields();
                importForm.setFieldsValue({ force: false });
                setImportOpen(true);
              }}
            >
              {t("snapshot.import", "导入")}
            </Button>
            <Button
              type="primary"
              icon={<PlusOutlined />}
              onClick={() => {
                createForm.resetFields();
                createForm.setFieldsValue({
                  scope: "single",
                  agent_ids: [selectedAgent || "default"],
                  include_secrets: false,
                  include_global: false,
                  exclude_sessions: false,
                  exclude_memory: false,
                });
                setCreateOpen(true);
              }}
            >
              {t("snapshot.create", "创建快照")}
            </Button>
          </Space>
        }
      />

      <Card className={styles.tableCard}>
        <Table<SnapshotRow>
          columns={columns}
          dataSource={rows}
          loading={loading}
          rowKey="snapshot_id"
          pagination={{ pageSize: 10 }}
        />
      </Card>

      <Modal
        title={t("snapshot.create", "创建快照")}
        open={createOpen}
        confirmLoading={creating}
        onCancel={() => setCreateOpen(false)}
        onOk={async () => {
          try {
            const values = await createForm.validateFields();
            setCreating(true);
            const payload: CreateSnapshotRequest = {
              ...values,
              note: values.note || "",
            };
            if (payload.scope === "single") {
              payload.agent_ids = [selectedAgent || "default"];
            } else if (payload.scope !== "selected") {
              delete payload.agent_ids;
            }
            if (payload.scope === "all") {
              payload.include_global = true;
            }
            await api.createSnapshot(payload);
            message.success(t("snapshot.createSuccess", "快照创建成功"));
            setCreateOpen(false);
            await loadSnapshots();
          } catch (err: unknown) {
            if (err instanceof Error) {
              message.error(err.message);
            }
          } finally {
            setCreating(false);
          }
        }}
      >
        <Form form={createForm} layout="vertical">
          <Form.Item
            name="scope"
            label={t("snapshot.scope", "范围")}
            rules={[{ required: true }]}
          >
            <Select
              options={[
                { value: "single", label: t("snapshot.scopeSingle", "当前 Agent") },
                { value: "selected", label: t("snapshot.scopeSelected", "指定 Agent") },
                { value: "all", label: t("snapshot.scopeAll", "所有 Agent") },
              ]}
            />
          </Form.Item>

          {scope === "selected" ? (
            <Form.Item
              name="agent_ids"
              label={t("snapshot.agents", "包含 Agent")}
              rules={[{ required: true, message: t("snapshot.agentRequired", "请选择至少一个 Agent") }]}
            >
              <Select mode="multiple" options={agentOptions.map((id) => ({ value: id, label: id }))} />
            </Form.Item>
          ) : null}

          <Form.Item name="note" label={t("snapshot.note", "备注")}>
            <Input placeholder={t("snapshot.notePlaceholder", "例如：修改 prompt 前备份")} />
          </Form.Item>

          <Form.Item label={t("snapshot.includeSecrets", "包含密钥信息")} name="include_secrets" valuePropName="checked">
            <Switch />
          </Form.Item>
          <Form.Item label={t("snapshot.includeGlobal", "包含全局配置")} name="include_global" valuePropName="checked">
            <Switch disabled={scope === "all"} />
          </Form.Item>
          <Form.Item label={t("snapshot.excludeSessions", "排除对话历史")} name="exclude_sessions" valuePropName="checked">
            <Switch />
          </Form.Item>
          <Form.Item label={t("snapshot.excludeMemory", "排除记忆目录")} name="exclude_memory" valuePropName="checked">
            <Switch />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={t("snapshot.restore", "恢复快照")}
        open={restoreModal.open}
        confirmLoading={restoring}
        onCancel={() => {
          setRestoreModal({ open: false, snapshot: null });
          restoreForm.resetFields();
        }}
        onOk={async () => {
          if (!restoreModal.snapshot) return;
          try {
            const values = await restoreForm.validateFields();
            setRestoring(true);
            const payload: RestoreSnapshotRequest = {
              agent_id: values.agent_id,
              mode: values.mode || "in_place",
              new_agent_id: values.new_agent_id,
            };
            if (payload.mode !== "clone") {
              delete payload.new_agent_id;
            }
            const result = await api.restoreSnapshot(
              restoreModal.snapshot.snapshot_id,
              payload,
            );
            message.success(result.message || t("snapshot.restoreSuccess", "恢复成功"));
            setRestoreModal({ open: false, snapshot: null });
            restoreForm.resetFields();
            await loadSnapshots();
          } catch (err: unknown) {
            if (err instanceof Error) {
              message.error(err.message);
            }
          } finally {
            setRestoring(false);
          }
        }}
      >
        <Alert
          type="warning"
          showIcon
          className={styles.warning}
          message={t(
            "snapshot.restoreWarning",
            "恢复只回滚本地状态，外部副作用（已发消息/已执行任务）不会撤销。",
          )}
        />
        <Form form={restoreForm} layout="vertical">
          <Form.Item
            name="agent_id"
            label={t("snapshot.targetAgent", "目标 Agent")}
            rules={[{ required: true }]}
          >
            <Select
              options={(restoreModal.snapshot?.agent_ids || agentOptions).map((id) => ({
                value: id,
                label: id,
              }))}
            />
          </Form.Item>
          <Form.Item name="mode" label={t("snapshot.restoreMode", "恢复模式")} initialValue="in_place">
            <Select
              options={[
                { value: "in_place", label: t("snapshot.modeInPlace", "原地恢复") },
                { value: "clone", label: t("snapshot.modeClone", "克隆为新 Agent") },
              ]}
            />
          </Form.Item>
          {Form.useWatch("mode", restoreForm) === "clone" ? (
            <Form.Item
              name="new_agent_id"
              label={t("snapshot.newAgentId", "新 Agent ID")}
              rules={[{ required: true, message: t("snapshot.newAgentRequired", "请输入新 Agent ID") }]}
            >
              <Input />
            </Form.Item>
          ) : null}
        </Form>
      </Modal>

      <Modal
        title={t("snapshot.import", "导入快照")}
        open={importOpen}
        confirmLoading={importing}
        onCancel={() => {
          setImportOpen(false);
          importForm.resetFields();
          setImportFile(null);
          setImportResult(null);
        }}
        onOk={async () => {
          if (!importFile) {
            message.warning(t("snapshot.fileRequired", "请先选择 ZIP 文件"));
            return;
          }
          try {
            const values = await importForm.validateFields();
            setImporting(true);
            const agentMapping = buildAgentMappingFromRows(values.mapping_rows);
            const result = await api.importSnapshot(importFile, {
              agentId: agentMapping ? undefined : values.agent_id?.trim() || undefined,
              force: !!values.force,
              agentMapping,
              password: values.password?.trim() || undefined,
            });
            setImportResult(result);
            message.success(t("snapshot.importSuccess", "导入成功"));
            await loadSnapshots();
            await loadAgents();
          } catch (err: unknown) {
            const msg = err instanceof Error ? err.message : t("snapshot.importFailed", "导入失败");
            message.error(msg);
          } finally {
            setImporting(false);
          }
        }}
      >
        <Form form={importForm} layout="vertical" initialValues={{ mapping_rows: [], force: false }}>
          <Form.Item label={t("snapshot.importFile", "快照文件")}>
            <Upload
              beforeUpload={(file) => {
                setImportFile(file);
                return false;
              }}
              maxCount={1}
              accept=".zip"
            >
              <Button icon={<UploadOutlined />}>{t("snapshot.selectFile", "选择 ZIP 文件")}</Button>
            </Upload>
            {importFile ? (
              <Typography.Text className={styles.fileName}>
                {importFile.name}
              </Typography.Text>
            ) : null}
          </Form.Item>
          <Form.Item label={t("snapshot.agentMappingTitle")}>
            <Typography.Paragraph type="secondary" className={styles.mappingHint}>
              {t("snapshot.agentMappingHint")}
            </Typography.Paragraph>
            <Form.List name="mapping_rows">
              {(fields, { add, remove }) => (
                <div className={styles.mappingList}>
                  {fields.map(({ key, name, ...restField }) => (
                    <Space key={key} align="baseline" className={styles.mappingRow}>
                      <Form.Item {...restField} name={[name, "source"]} style={{ marginBottom: 0 }}>
                        <Input placeholder={t("snapshot.mappingSource")} style={{ width: 160 }} />
                      </Form.Item>
                      <span className={styles.mappingArrow}>→</span>
                      <Form.Item {...restField} name={[name, "target"]} style={{ marginBottom: 0 }}>
                        <Input placeholder={t("snapshot.mappingTarget")} style={{ width: 160 }} />
                      </Form.Item>
                      <MinusCircleOutlined
                        className={styles.mappingRemove}
                        onClick={() => remove(name)}
                      />
                    </Space>
                  ))}
                  <Button type="dashed" onClick={() => add()} block icon={<PlusOutlined />}>
                    {t("snapshot.addMappingRow")}
                  </Button>
                </div>
              )}
            </Form.List>
          </Form.Item>
          <Form.Item name="agent_id" label={t("snapshot.importAgentId", "导入为 Agent ID（可选）")}>
            <Input placeholder={t("snapshot.importAgentHint", "留空则使用快照中的 Agent ID")} />
          </Form.Item>
          <Form.Item name="force" label={t("snapshot.forceOverwrite", "覆盖同名 Agent")} valuePropName="checked">
            <Switch />
          </Form.Item>
          <Form.Item name="password" label={t("snapshot.importPassword")}>
            <Input.Password
              autoComplete="new-password"
              placeholder={t("snapshot.importPasswordPlaceholder")}
            />
          </Form.Item>
        </Form>
        {primaryImportResult ? (
          <Card size="small" className={styles.importResultCard}>
            <Typography.Text strong>
              {t("snapshot.importResult", "导入结果")}
            </Typography.Text>
            <div className={styles.importResultRow}>
              <span>{t("snapshot.status", "状态")}:</span>
              <Tag color={primaryImportResult.status === "ready" ? "green" : primaryImportResult.status === "needs_setup" ? "red" : "gold"}>
                {formatSnapshotStatus(primaryImportResult.status, t)}
              </Tag>
            </div>
            {normalizedImportResults.length > 1 ? (
              <div className={styles.perAgentResults}>
                <Typography.Text type="secondary">{t("snapshot.agents")}</Typography.Text>
                <Table<ImportSingleResult>
                  size="small"
                  pagination={false}
                  rowKey={(row, index) => `${row.agent_id}-${index ?? 0}`}
                  dataSource={normalizedImportResults}
                  columns={[
                    {
                      title: t("snapshot.agents"),
                      dataIndex: "agent_id",
                    },
                    {
                      title: t("snapshot.status", "状态"),
                      dataIndex: "status",
                      width: 140,
                      render: (st: AgentStatus) => (
                        <Tag color={st === "ready" ? "green" : st === "needs_setup" ? "red" : "gold"}>
                          {formatSnapshotStatus(st, t)}
                        </Tag>
                      ),
                    },
                    {
                      title: t("snapshot.fileSummary", "导入内容概要"),
                      render: (_, row) =>
                        row.file_summary && Object.keys(row.file_summary).length > 0
                          ? `${Object.keys(row.file_summary).length}`
                          : "0",
                    },
                  ]}
                />
              </div>
            ) : null}
            {importAgentRows.length > 0 ? (
              <div className={styles.perAgentResults}>
                <Typography.Text type="secondary">{t("snapshot.perAgentResults")}</Typography.Text>
                <Table<ImportAgentResultItem>
                  size="small"
                  pagination={false}
                  rowKey={(row, index) => importAgentResultKey(row, index)}
                  dataSource={importAgentRows}
                  columns={[
                    {
                      title: t("snapshot.perAgentMappingCol"),
                      key: "map",
                      render: (_, row) => {
                        const { source, target } = importAgentResultSourceTarget(row);
                        return (
                          <span>
                            {source} → {target}
                          </span>
                        );
                      },
                    },
                    {
                      title: t("snapshot.status", "状态"),
                      dataIndex: "status",
                      width: 120,
                      render: (st: string | undefined) => {
                        if (st === "ready" || st === "needs_setup" || st === "needs_review") {
                          return (
                            <Tag
                              color={
                                st === "ready" ? "green" : st === "needs_setup" ? "red" : "gold"
                              }
                            >
                              {formatSnapshotStatusLoose(st, t)}
                            </Tag>
                          );
                        }
                        return st ?? "—";
                      },
                    },
                    {
                      title: t("snapshot.perAgentMessageCol"),
                      dataIndex: "message",
                      ellipsis: true,
                      render: (m: string | undefined) => m || "—",
                    },
                  ]}
                />
              </div>
            ) : null}
            {primaryImportResult.file_summary &&
            Object.keys(primaryImportResult.file_summary).length > 0 ? (
              <div className={styles.fileSummary}>
                <Typography.Text type="secondary">
                  {t("snapshot.fileSummary", "导入内容概要")}
                </Typography.Text>
                <div className={styles.fileSummaryRows}>
                  {Object.entries(primaryImportResult.file_summary).map(([k, v]) => (
                    <div key={k} className={styles.fileSummaryRow}>
                      <span className={styles.fileSummaryKey}>
                        {t(`snapshot.fileSummaryKeys.${k}`, { defaultValue: k })}
                      </span>
                      <span>{v}</span>
                    </div>
                  ))}
                </div>
              </div>
            ) : null}
            <div className={styles.importTodos}>
              {(primaryImportResult.todos ?? []).map((todo, idx) => (
                <Alert
                  key={`${todo.message}-${idx}`}
                  type={todo.severity === "required" ? "error" : "warning"}
                  showIcon
                  message={todo.message}
                  description={todo.action}
                />
              ))}
            </div>
          </Card>
        ) : null}
      </Modal>

      <Modal
        title={t("snapshot.exportConfirmTitle")}
        open={exportDialog.open}
        confirmLoading={
          !!exportDialog.snapshot && downloadingId === exportDialog.snapshot.snapshot_id
        }
        okText={t("snapshot.exportDownload")}
        onCancel={() => {
          setExportDialog({ open: false, snapshot: null });
          setExportPassword("");
        }}
        onOk={async () => {
          const row = exportDialog.snapshot;
          if (!row) return;
          try {
            setDownloadingId(row.snapshot_id);
            const { blob, filename } = await api.exportSnapshot(
              row.snapshot_id,
              false,
              exportPassword.trim() || undefined,
            );
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            a.download = filename;
            document.body.appendChild(a);
            a.click();
            a.remove();
            URL.revokeObjectURL(url);
            message.success(t("snapshot.exportSuccess", "导出成功"));
            setExportDialog({ open: false, snapshot: null });
            setExportPassword("");
          } catch (err: unknown) {
            const msg =
              err instanceof Error ? err.message : t("snapshot.exportFailed", "导出失败");
            message.error(msg);
          } finally {
            setDownloadingId("");
          }
        }}
      >
        <Typography.Paragraph type="secondary" className={styles.exportPasswordHint}>
          {t("snapshot.exportPasswordHint")}
        </Typography.Paragraph>
        <Input.Password
          value={exportPassword}
          onChange={(e) => setExportPassword(e.target.value)}
          placeholder={t("snapshot.exportPasswordPlaceholder")}
          autoComplete="new-password"
        />
      </Modal>
    </div>
  );
}

