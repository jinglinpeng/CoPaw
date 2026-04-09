# 测试结果与复现指南

## 1. 本次已完成测试

测试目录：`tests/snapshot/`

- `test_packer.py`
- `test_collector.py`
- `test_sanitizer.py`
- `test_quarantine.py`
- `test_restorer.py`

执行命令：

```powershell
& "d:\projects\CoPaw\.venv\Scripts\python.exe" -m pytest tests/snapshot/ -v --tb=short
```

结果：`21 passed`

## 2. 测试覆盖点

### 2.1 Packer

- ZIP 打包/解包 roundtrip
- manifest 读取
- 非 ZIP 拒绝
- 缺失 manifest 拒绝
- checksum 路径覆盖

### 2.2 Collector

- workspace 收集
- 排除 sessions/memory 选项
- global 收集
- 大小估算

### 2.3 Sanitizer

- `agent.json` 敏感字段脱敏
- 导出时 `secrets/` 目录移除

### 2.4 Quarantine

- skills 全部禁用
- jobs 全部禁用
- channels 标记 `_imported_disabled`
- mcp 标记 `_imported_disabled`

### 2.5 Restorer

- `prepare` 生成状态
- `apply` 目录切换
- `rollback` 回滚
- 崩溃恢复分支（关键步骤）

## 3. 建议回归顺序（接手人）

1. 先跑单测：
   - `tests/snapshot/`
2. 再跑 API 冒烟（手工或脚本）：
   - create -> list -> get -> export -> delete
3. 最后跑 UI 集成点击：
   - `/snapshot` 全链路

## 4. 快速 API 冒烟（可选）

可用 `curl`/Postman/前端页面执行下列路径：

- `POST /api/snapshots`
- `GET /api/snapshots`
- `GET /api/snapshots/{id}`
- `POST /api/snapshots/{id}/restore`
- `GET /api/snapshots/{id}/export`
- `POST /api/snapshots/import`
- `DELETE /api/snapshots/{id}`

## 5. 测试局限（当前尚未覆盖）

- 加密导出/导入（未实现）
- 多 agent 映射导入策略（仅首个 agent）
- 真正并发场景下与 manager reload 的竞态
- 前端自动化回归（当前以人工点击为主）
