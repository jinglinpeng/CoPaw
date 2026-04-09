# Snapshot 功能交接总览

## 1) 当前状态（给接手人先看）

- 后端第一版已落地：`src/copaw/app/snapshot/` 已有可运行实现。
- FastAPI 已接入：`/api/snapshots` 路由已注册，`SnapshotManager` 已在应用启动时初始化。
- 单测已补齐并通过：`tests/snapshot/` 共 21 条（全部通过）。
- 前端页面已存在：`console/src/pages/Settings/Snapshot/`，侧边栏入口与路由已可达。
- 本目录已有阶段日志：`2026-04-09-progress.md`。

## 2) 5 分钟上手路径

1. 先读 `01-backend-implementation-map.md`（知道代码在哪、模块怎么连）。
2. 再读 `07-next-steps-priority.md`（知道先做什么、验收标准是什么）。
3. 启动环境按 `03-runtime-and-startup-guide.md`。
4. 跑回归按 `04-test-results-and-reproduce.md`。
5. 做 UI 联调按 `05-ui-integration-test-playbook.md`。

## 3) 本次交接文档索引

- `00-handoff-overview.md`：总览（本文件）
- `01-backend-implementation-map.md`：后端实现地图
- `02-api-contract-and-examples.md`：接口契约与示例
- `03-runtime-and-startup-guide.md`：本地运行手册
- `04-test-results-and-reproduce.md`：测试结果与复现
- `05-ui-integration-test-playbook.md`：UI 集成测试剧本
- `06-known-issues-and-risks.md`：已知问题与风险
- `07-next-steps-priority.md`：下一步优先级与验收
- `08-change-log-2026-04-09.md`：本次改动清单

## 4) 接手建议

- 第一优先：先把 `06-known-issues-and-risks.md` 中 P0/P1 补齐，再扩展功能。
- 第二优先：完成 UI 剧本中的全链路验证（创建/恢复/导入/导出/删除）。
- 第三优先：补 CLI 与加密导出（设计文档里有要求，当前实现未覆盖）。

