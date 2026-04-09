# 下一步任务优先级与验收标准

## P0（立即执行）

### P0-1 并发锁统一到 MultiAgentManager

- 目标：
  - 将 snapshot 相关操作与 `reload_agent/stop_agent` 放入统一 op lock 域
- 产出：
  - manager 层新增并公开 per-agent op lock
  - snapshot manager 改为调用 manager lock
- 验收：
  - 并发触发 restore + reload 不出现双实例冲突
  - 有对应并发测试或集成复现实验记录

### P0-2 多 agent 导入映射

- 目标：
  - 导入多 agent 包时支持 ID 映射表
- 产出：
  - API 入参支持映射结构
  - 前端导入页支持映射编辑
- 验收：
  - 多 agent 包导入后，映射目标与实际目录一致
  - 冲突命名处理可预测

### P0-3 清理 manager.py 结构债务

- 目标：
  - 清理无效导入和易混淆路径
  - 提升可读性
- 验收：
  - lints 全绿
  - 关键路径行为不变（回归测试通过）

## P1（第二阶段）

### P1-1 加密导出/导入（AES-256-GCM）

- 目标：
  - 支持密码加密包的导出与导入
- 产出：
  - 后端加解密模块
  - UI 密码输入步骤
  - 错误提示统一为“密码错误或文件损坏/被篡改”
- 验收：
  - 密码正确可导入
  - 密码错误、密文损坏、版本不支持都能正确报错

### P1-2 任务进度接口

- 目标：
  - create/import/restore 提供可查询进度
- 产出：
  - task_id + status API（或 SSE）
  - 前端进度条联动
- 验收：
  - 大文件操作期间前端可持续显示阶段进度

### P1-3 Session 兼容预扫描增强

- 目标：
  - 导入阶段报告“可用/不兼容会话数”
- 验收：
  - 导入结果中明确显示统计与原因

## P2（可延后）

### P2-1 i18n 补齐

- 补齐 `snapshot.*` 文案键，减少 fallback 文案

### P2-2 CLI 命令集接入

- `copaw snapshot create/list/restore/export/import/delete/prune`

### P2-3 清理与保留策略

- backup/snapshot 的保留策略与清理入口

## 推荐执行顺序（接手人）

1. 先做 P0-1（锁域统一）
2. 再做 P0-2（多 agent 映射）
3. 然后进入 P1-1（加密）
4. 最后补 P1-2（进度）和 P1-3（兼容扫描）

## 每阶段 Done 定义

- 代码提交 + 单测通过
- UI 手工验证至少 1 条主流程 + 1 条失败流程
- 更新本目录对应文档（变更、风险、测试结果）
