# 本地运行与启动手册（Snapshot 联调）

## 1. 环境前置

- 操作系统：Windows（当前交接环境）
- 项目根目录：`d:\projects\CoPaw`
- Python 虚拟环境：`d:\projects\CoPaw\.venv`
- Node 前端目录：`d:\projects\CoPaw\console`

## 2. 后端启动步骤

### 2.1 安装（editable）

```powershell
& "d:\projects\CoPaw\.venv\Scripts\pip.exe" install -e .
```

### 2.2 端口检查（8088）

```powershell
Get-NetTCPConnection -LocalPort 8088 -State Listen -ErrorAction SilentlyContinue
```

如有占用，先定位 PID 再结束：

```powershell
Stop-Process -Id <PID> -Force
```

### 2.3 设置 CORS 并启动

```powershell
$env:COPAW_CORS_ORIGINS="http://localhost:5173"
& "d:\projects\CoPaw\.venv\Scripts\copaw.exe" app --host 127.0.0.1 --port 8088
```

启动成功标志：

- 日志出现 `Uvicorn running on`（或等价启动日志）
- 可访问 `http://127.0.0.1:8088/api/version`

## 3. 前端启动步骤

### 3.1 安装依赖

> 在该环境中建议用 `npm.cmd`（PowerShell 脚本限制会影响 `npm`）

```powershell
cd d:\projects\CoPaw\console
npm.cmd ci
```

### 3.2 启动开发服务

```powershell
npm.cmd run dev
```

启动成功标志：

- 终端出现 `Local: http://localhost:5173/`
- 浏览器打开 `http://localhost:5173` 可见 Console 页面

## 4. 联调地址

- 前端：`http://localhost:5173`
- 后端：`http://127.0.0.1:8088`
- 快照页路由：`http://localhost:5173/snapshot`

## 5. 常见问题排查

### 5.1 `python -m pytest` 提示无 pytest

- 原因：没有使用项目 `.venv`
- 解决：统一使用
  - `& "d:\projects\CoPaw\.venv\Scripts\python.exe" -m pytest ...`

### 5.2 `npm ci` 报 PowerShell ConstrainedLanguage

- 原因：`npm.ps1` 受策略限制
- 解决：使用 `npm.cmd ci` / `npm.cmd run dev`

### 5.3 前端能开但接口 4xx/5xx

- 检查后端是否已设置 `COPAW_CORS_ORIGINS=http://localhost:5173`
- 检查后端是否在 `127.0.0.1:8088` 正常运行
- 检查 SnapshotManager 是否初始化（看后端启动日志）

### 5.4 快照接口不生效

- 检查 `src/copaw/app/routers/__init__.py` 是否包含 `snapshot_router`
- 检查 `src/copaw/app/_app.py` 是否设置 `app.state.snapshot_manager`
