# Snapshot API 契约与示例

## 1. 基础信息

- 基础前缀：`/api/snapshots`
- 认证：沿用现有全局认证中间件（若开启）
- 内容类型：
  - JSON：创建、恢复、删除、列表、详情
  - `multipart/form-data`：导入
  - `application/zip`：导出

## 2. 接口列表

### 2.1 创建快照

- `POST /api/snapshots`

请求体示例：

```json
{
  "scope": "selected",
  "agent_ids": ["default", "qa-agent"],
  "include_secrets": false,
  "include_global": true,
  "exclude_sessions": false,
  "exclude_memory": false,
  "note": "修改 prompt 前备份"
}
```

响应示例：

```json
{
  "snapshot_id": "copaw-snapshot-selected-20260409120000",
  "filename": "copaw-snapshot-selected-20260409120000.zip",
  "scope": "selected",
  "agent_ids": ["default", "qa-agent"],
  "created_at": "2026-04-09T12:00:00+00:00",
  "size_bytes": 123456,
  "includes_secrets": false,
  "includes_global": true,
  "notes": "修改 prompt 前备份"
}
```

### 2.2 列表

- `GET /api/snapshots`

响应：`SnapshotInfo[]`

### 2.3 详情

- `GET /api/snapshots/{snapshot_id}`

响应：`SnapshotInfo`

### 2.4 删除

- `DELETE /api/snapshots/{snapshot_id}`

响应示例：

```json
{
  "success": true,
  "snapshot_id": "copaw-snapshot-default-20260409120000"
}
```

### 2.5 恢复

- `POST /api/snapshots/{snapshot_id}/restore`

请求体示例（原地恢复）：

```json
{
  "agent_id": "default",
  "mode": "in_place"
}
```

请求体示例（克隆）：

```json
{
  "agent_id": "default",
  "mode": "clone",
  "new_agent_id": "default-exp"
}
```

响应示例：

```json
{
  "success": true,
  "agent_id": "default",
  "mode": "in_place",
  "message": "已恢复到快照 copaw-snapshot-default-20260409120000"
}
```

### 2.6 导出

- `GET /api/snapshots/{snapshot_id}/export?include_secrets=false`

说明：

- 返回 ZIP 文件流
- 文件名在 `Content-Disposition`
- 当前实现 `include_secrets=true` 时会返回原快照文件

### 2.7 导入

- `POST /api/snapshots/import?agent_id=<id>&force=false`
- Body：`multipart/form-data`，字段名 `file`

响应示例：

```json
{
  "agent_id": "customer-service",
  "status": "needs_review",
  "file_summary": {
    "agent_config": "✓",
    "skills_quarantined": "3",
    "jobs_quarantined": "2",
    "channels_quarantined": "2",
    "mcp_quarantined": "1",
    "sessions": "8"
  },
  "todos": [
    {
      "severity": "suggested",
      "message": "2 个渠道待重新认证",
      "action": "在 Console -> Channels 中配置"
    }
  ]
}
```

## 3. 类型约束（前后端字段对齐）

- `scope`: `"single" | "selected" | "all"`
- `mode`: `"in_place" | "clone"`
- `status`: `"ready" | "needs_review" | "needs_setup"`
- `todo.severity`: 目前输出 `"required"` 或 `"suggested"`

## 4. 常见错误码

- `400`
  - 参数不合法
  - 快照不存在
  - 恢复目标 agent 不存在
  - 导入目标冲突且未 `force`
- `404`
  - 查询或删除不存在的快照
- `500`
  - SnapshotManager 未初始化
  - 打包/解包/文件系统异常

## 5. 前端调用实现位置

- API 模块：`console/src/api/modules/snapshot.ts`
- 类型定义：`console/src/api/types/snapshot.ts`
- 页面：`console/src/pages/Settings/Snapshot/index.tsx`

## 6. 当前契约与设计文档差异（接手前必看）

- 当前未实现加密导出/导入密码链路（仅保留 `include_secrets` 分支）
- 当前未实现多 agent 映射导入（默认取快照内第一个 agent 或显式 `agent_id`）
- 当前未实现任务进度查询接口（长任务为同步等待返回）
