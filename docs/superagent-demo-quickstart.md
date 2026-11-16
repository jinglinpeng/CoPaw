# QwenPaw 本地启动速查

> 面向已经装好 QwenPaw、但还不确定“先敲哪条命令”的新同事。本文命令按下述 fork `main` 分支源码逐条核对：撰写基线为 commit `502b59d`、包版本 `2.3.0b1`。文中只做静态源码核实，不代表实际运行结果；真实环境以 `qwenpaw --help` 输出为准。

## 1. 四条基础命令

| 命令 | 用途 | 核对要点 |
| --- | --- | --- |
| `qwenpaw --version` | 确认已安装的是 QwenPaw 以及版本号 | 版本号取自 `src/qwenpaw/__version__.py`；`copaw` 是同一入口的别名 |
| `qwenpaw --help` | 查看全部子命令与全局参数 | 等价于 `-h`；帮助由 Click 在进入主回调前处理 |
| `qwenpaw init --defaults` | 用默认值创建工作目录和配置 | 生成 `config.json`、`HEARTBEAT.md` 与默认 workspace，跳过交互向导 |
| `qwenpaw app --host 127.0.0.1 --port 8088` | 启动本地 Web 控制台 / API 服务 | `app` 的默认 host/port 就是 `127.0.0.1:8088`，因此等价于 `qwenpaw app` |

`qwenpaw app` 启动后，在浏览器打开 <http://127.0.0.1:8088/> 即可进入控制台。该命令是前台进程，会占用当前终端，按 `Ctrl+C` 结束。

### `--host` / `--port` 属于哪一层？

任务给出的示例与当前源码一致，但参数层级容易写错，核对结果如下：

- `app` 子命令自己定义了 `--host`（默认 `127.0.0.1`）和 `--port`（默认 `8088`），并直接传给 uvicorn 绑定服务，见 [`src/qwenpaw/cli/app_cmd.py`](../src/qwenpaw/cli/app_cmd.py#L82-L95) 与同文件的 `uvicorn.run(...)`。
- 顶层 `qwenpaw` 也有同名 `--host` / `--port`，见 [`src/qwenpaw/cli/main.py`](../src/qwenpaw/cli/main.py#L188-L199)。这是给 `cron`、`doctor` 等**客户端子命令**连接“已在运行的服务”用的（[`src/qwenpaw/cli/http.py`](../src/qwenpaw/cli/http.py#L29-L45)），不会改变 `app` 监听的地址。
- 所以正确写法是 `qwenpaw app --host 127.0.0.1 --port 8088`；写成 `qwenpaw --host 0.0.0.0 --port 9000 app` **不会**把服务绑到 `0.0.0.0:9000`，`app` 仍使用它自己的默认值。

## 2. 不带参数的 `qwenpaw` 会发生什么

直接运行 `qwenpaw`（不加任何子命令或参数）会进入**交互式终端聊天界面（TUI）**，而不是打印帮助：

- CLI 根命令是一个 `invoke_without_command=True` 的 Click group；当 `ctx.invoked_subcommand is None` 时，会调用 `run_tui(...)` 打开 TUI（[`src/qwenpaw/cli/main.py`](../src/qwenpaw/cli/main.py#L131-L134)、[`src/qwenpaw/cli/main.py`](../src/qwenpaw/cli/main.py#L214-L221)）。
- TUI 会占住当前终端，直到退出后才回到 shell。所以查看帮助必须显式加 `--help`（或 `-h`）：`qwenpaw --help` 由 Click 在进入主回调之前处理，不会打开 TUI。
- 同理，`qwenpaw --version` 也只打印版本、不会进入 TUI。
- 附带一点：`qwenpaw .` 或 `qwenpaw <目录>` 也会进入 TUI，并把该目录作为项目上下文；不要把它误当成“启动服务”的命令——启动服务用 `qwenpaw app`。

## 3. 建议的最小启动顺序

```bash
qwenpaw --version                          # 1. 确认装的是哪个版本
qwenpaw --help                             # 2. 看一眼可用命令
qwenpaw init --defaults                    # 3. 首次使用：生成默认配置
qwenpaw app --host 127.0.0.1 --port 8088   # 4. 启动控制台
```

然后再打开 <http://127.0.0.1:8088/> 配置模型（见第 4 节）。

初始化前有两件事值得先确认：

- **工作目录**由 `QWENPAW_WORKING_DIR` 环境变量决定；未设置时优先沿用旧的 `~/.copaw`，否则用 `~/.qwenpaw`（[`src/qwenpaw/constant.py`](../src/qwenpaw/constant.py#L82-L94)）。如果本机已有日常使用的配置、只想演示一次，可先 `export QWENPAW_WORKING_DIR=/tmp/qwenpaw-demo` 再执行 `init` / `app`，避免动到原配置。
- `init --defaults` 并不是“纯检查”命令：它会按默认值重新写入配置并初始化 workspace，已有 `config.json` 时建议先备份或换临时工作目录。另外它仍会展示安全提示并要求确认一次；源码里另有 `--accept-security`，与 `--defaults` 同用可完全跳过该确认（[`src/qwenpaw/cli/init_cmd.py`](../src/qwenpaw/cli/init_cmd.py#L197-L215)）。
- `--defaults` 在还没有配置模型时会明确提示 “No LLM provider configured”，只负责准备配置骨架，不会替你填 API Key。

## 4. 三条常见排障

### 4.1 `qwenpaw: command not found`

- 一键安装脚本会把可执行入口放在 `~/.qwenpaw/bin`，并尝试写入 shell profile 的 `PATH`（[`scripts/install.sh`](../scripts/install.sh#L316-L325)）。安装后请新开一个终端，或 `source ~/.bashrc` / `source ~/.zshrc`；用 `echo $PATH` 确认其中包含 `~/.qwenpaw/bin`。
- 不想改 `PATH` 时，可直接用绝对路径验证：`~/.qwenpaw/bin/qwenpaw --version`。
- Windows 上对应目录是 `%USERPROFILE%\.qwenpaw\bin`，需要加入系统或用户 `Path`；若是 pip / 虚拟环境安装，则入口位于该环境的 `bin`（macOS/Linux）或 `Scripts`（Windows）目录。

### 4.2 端口已被占用

- `qwenpaw app` 会把端口原样交给 uvicorn，源码没有“自动换端口”逻辑，端口冲突时启动会报 `Address already in use`（Linux 上常见 `[Errno 98]`）。处理方式三选一：
  - macOS / Linux：`lsof -i :8088`（或 `ss -ltnp | grep 8088`）找到占用进程并停止；
  - Windows：`netstat -ano | findstr :8088`，再按 PID 结束对应进程；
  - 临时换端口启动：`qwenpaw app --port 8089`，同时把浏览器地址改成 <http://127.0.0.1:8089/>。
- 不要用 `qwenpaw --port 8089 app` 来换端口：如第 1 节所述，放在子命令前的 `--port` 不会作用于 `app`。

### 4.3 模型或账号尚未配置

- `qwenpaw init --defaults` 只准备默认配置与 workspace；没有配置 LLM 提供商 / API Key 时，控制台页面可以打开，但对话还无法真正调用模型。初始化成功 ≠ 模型可用。
- 配置入口：
  - 控制台：<http://127.0.0.1:8088/> → 设置 → 模型，选择提供商、填写 API Key 并启用模型；
  - 命令行：`qwenpaw models config`（完整交互式配置）或 `qwenpaw models list`（先看当前状态），见 [`src/qwenpaw/cli/providers_cmd.py`](../src/qwenpaw/cli/providers_cmd.py#L366-L458)。
- 使用本地模型（如 Ollama / LM Studio / llama.cpp）时无需云端 API Key，但仍需先安装对应应用，并在配置中选定本地提供商与模型。

## 5. 源码核对路径

以下路径均以本 fork `main` 分支（撰写基线 commit `502b59d`）为准，便于现场从文档直接跳到实现：

| 想确认什么 | 源码路径 |
| --- | --- |
| CLI 入口、`--version` / `--help`、裸命令进入 TUI、顶层 `--host` / `--port` | [`src/qwenpaw/cli/main.py`](../src/qwenpaw/cli/main.py) |
| `qwenpaw app` 的 `--host` / `--port` 定义与 uvicorn 启动参数 | [`src/qwenpaw/cli/app_cmd.py`](../src/qwenpaw/cli/app_cmd.py) |
| `qwenpaw init` 的 `--force` / `--defaults` / `--accept-security` 与写盘内容 | [`src/qwenpaw/cli/init_cmd.py`](../src/qwenpaw/cli/init_cmd.py) |
| 裸 `qwenpaw` 打开的终端聊天界面 | [`src/qwenpaw/cli/tui/launch.py`](../src/qwenpaw/cli/tui/launch.py) |
| 工作目录优先级（`QWENPAW_WORKING_DIR` / `~/.copaw` / `~/.qwenpaw`） | [`src/qwenpaw/constant.py`](../src/qwenpaw/constant.py) |
| 版本号来源（当前为 `2.3.0b1`） | [`src/qwenpaw/__version__.py`](../src/qwenpaw/__version__.py) |
| 命令入口注册与 `copaw` 别名 | [`pyproject.toml`](../pyproject.toml) |
| 客户端子命令如何用全局 `--host` / `--port` 定位服务 | [`src/qwenpaw/cli/http.py`](../src/qwenpaw/cli/http.py) |
| 模型 / API Key 配置命令 | [`src/qwenpaw/cli/providers_cmd.py`](../src/qwenpaw/cli/providers_cmd.py) |
| 安装脚本把 `qwenpaw` 放到哪、如何改 `PATH` | [`scripts/install.sh`](../scripts/install.sh) |
| 更完整的 CLI 命令参考（仓库文档） | [`website/public/docs/cli.zh.md`](../website/public/docs/cli.zh.md) |
