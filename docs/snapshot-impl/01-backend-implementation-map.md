# Snapshot 后端实现地图

## 1. 模块入口与装配

- `src/copaw/app/_app.py`
  - 在应用 `lifespan` 中初始化 `SnapshotManager`
  - 将实例挂载到 `app.state.snapshot_manager`
  - 启动时执行 `snapshot_manager.check_crash_recovery()`
- `src/copaw/app/routers/__init__.py`
  - 注册 `snapshot_router`（来自 `src/copaw/app/snapshot/api.py`）
  - 对外暴露 `/api/snapshots/*` 路由

## 2. 核心目录结构（`src/copaw/app/snapshot/`）

- `models.py`
  - Pydantic 数据结构定义
  - 包含 `SnapshotManifest`、`SnapshotInfo`、`CreateSnapshotRequest`、`RestoreSnapshotRequest`、`ImportResult` 等
  - 枚举：`SnapshotScope`、`RestoreMode`、`AgentStatus`、`RestorePhase`

- `api.py`
  - FastAPI REST 路由层
  - 负责参数接收、HTTP 错误映射、与 `SnapshotManager` 对接

- `manager.py`
  - 顶层编排器（create/list/get/delete/restore/export/import）
  - 维护 per-agent 异步操作锁（`_op_locks`）
  - 协调 `StateCollector` / `SnapshotPacker` / `SnapshotRestorer` / `ImportQuarantine` / `HealthChecker`

- `collector.py`
  - 收集 workspace/global/secrets 到 staging 目录
  - 支持 `exclude_sessions` / `exclude_memory`
  - 支持体积估算 `estimate_size()`

- `packer.py`
  - ZIP 打包、解包、manifest 写入和读取
  - 文件 SHA-256 校验
  - 导入资源限制校验（文件数、总大小、单文件大小、压缩比、路径安全）

- `restorer.py`
  - 三阶段恢复状态机：`prepare -> apply -> verify`
  - 状态文件持久化路径：`{WORKING_DIR}/_restore_state/{agent_id}.json`
  - 支持崩溃恢复分支判断

- `sanitizer.py`
  - 导出脱敏逻辑
  - 递归将敏感字段替换为 `<REDACTED>`
  - 不含密钥导出时会删除 `secrets/` 目录

- `quarantine.py`
  - 不可信导入后静默态处理
  - `skills` 全部禁用、`jobs` 全部禁用
  - `channels` / `mcp` 写入 `_imported_disabled: true`

- `health.py`
  - 导入后可运行性检查
  - 产出状态：`ready` / `needs_review` / `needs_setup`
  - 输出待办清单（`todos`）

## 3. 关键运行时目录

- 本地快照：`{WORKING_DIR}/snapshots/*.zip`
- 恢复状态：`{WORKING_DIR}/_restore_state/*.json`
- 恢复临时目录：`{WORKING_DIR}/_restore_staging/`
- workspace：`{WORKING_DIR}/workspaces/{agent_id}/`
- 机密目录：`{SECRET_DIR}/`

## 4. 关键调用链

### 4.1 创建快照

1. API `POST /api/snapshots`
2. `SnapshotManager.create()`
3. `StateCollector.collect_workspace()/collect_global()/collect_secrets()`
4. `SnapshotPacker.pack()` 生成 ZIP + manifest + checksum
5. 返回 `SnapshotInfo`

### 4.2 恢复快照（原地）

1. API `POST /api/snapshots/{id}/restore`
2. `SnapshotManager.restore()`
3. `SnapshotRestorer.prepare()`（解压+校验）
4. `SnapshotRestorer.apply()`（备份旧目录并替换）
5. Manager 重启 workspace 做 verify
6. verify 失败时 `SnapshotRestorer.rollback()`

### 4.3 导入快照

1. API `POST /api/snapshots/import`
2. `SnapshotManager.import_snapshot()`
3. `SnapshotPacker.unpack(validate=True)`
4. `ImportQuarantine.quarantine()`（静默态）
5. 写入新 workspace + 注册 agent profile
6. `HealthChecker.check()` 产出状态与 todo

## 5. 当前实现风格

- 设计偏“模块隔离”而非深侵入现有架构
- 关键 I/O 操作使用 `asyncio.to_thread` 避免阻塞事件循环
- 错误传播策略：管理层抛 `ValueError`，API 层转换为 `HTTPException`
- 锁策略：当前是 `SnapshotManager` 内部 op lock（尚未改造 `MultiAgentManager` 原生 op lock）

## 6. 对接注意事项

- 前端依赖字段命名为 snake_case（与后端 Pydantic 字段一致）
- `SnapshotInfo.snapshot_id` 使用 zip 文件 stem（不是 UUID）
- 导入接口使用 multipart，query 参数为 `agent_id` / `force`
- 导出接口返回 `FileResponse`，文件名从 `Content-Disposition` 读取
