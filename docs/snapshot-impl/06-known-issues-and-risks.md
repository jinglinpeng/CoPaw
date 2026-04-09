# 已知问题与风险清单

## P0（接手后优先处理）

### 1) Manager 中仍有冗余/不一致导入

- 文件：`src/copaw/app/snapshot/manager.py`
- 现象：`_import_impl` 内有 `AgentProfileConfig` 的导入痕迹，但实际写入使用 `AgentProfileRef`
- 风险：后续重构时容易误用错误类型，降低可维护性
- 建议：清理无效导入，统一 profile 写入类型

### 2) per-agent lock 目前在 SnapshotManager 内部实现

- 现状：未改造 `MultiAgentManager` 原生生命周期锁
- 风险：与 `reload_agent()` 的极端并发竞态仍可能存在边角行为
- 建议：将 op lock 下沉到 `MultiAgentManager`，对 reload/stop/snapshot/restore/import/export 统一串行化

### 3) 导入多 agent 包仅使用第一个 agent

- 现状：`import_snapshot` 默认取 `manifest.agent_ids[0]`
- 风险：多 agent 包导入语义不完整，可能与 UI 预期不一致
- 建议：补齐映射策略（原 ID -> 新 ID），并在 API/前端同步

## P1（建议尽快补）

### 4) 加密导出/导入未落地

- 设计要求有 AES-256-GCM 密码链路
- 当前仅有 `include_secrets` 逻辑，未实现密码加密
- 建议：新增 `encrypt/decrypt` 模块 + UI 密码步骤 + 单测

### 5) 进度/长任务状态接口未实现

- 当前 create/import/restore 为请求内同步执行
- 风险：大文件或慢盘情况下前端体验差，可能超时
- 建议：引入任务 ID + 进度查询（或 SSE）

### 6) 导入兼容性预扫描仍较简化

- 当前主要做 JSON/文件级别检查
- 风险：会话深层兼容问题可能延后到运行期才暴露
- 建议：补充 session 结构探测与更细化报告

## P2（可后续迭代）

### 7) UI 国际化不完整（已部分修复）

- 已补：`nav.snapshot` 多语言键
- 未补：`snapshot.*` 页面文案大多依赖 fallback
- 建议：系统性补齐 `snapshot.*` i18n 文案

### 8) CLI 命令尚未接入

- 当前实现主要走 REST + Console
- 设计包含 `copaw snapshot ...` 完整命令集
- 建议：后续补 CLI 子命令层与帮助文档

## 运行风险说明

- 导入 `force=true` 会重命名旧 workspace 为 `.backup.<ts>`
- 当前默认不自动清理 backup 目录（避免误删）
- 建议后续增加 prune 策略和可视化提示

## 结论

- 当前版本可用于第一轮联调和功能演示
- 若要进入“可稳定上线”阶段，至少需要完成 P0 + P1 前三项
