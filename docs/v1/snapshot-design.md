# CoPaw Snapshot 功能方案设计

> 备份自 Cursor Plan，日期: 2026-04-08

## 一、状态全景分析

从第一性原理出发，我们首先需要回答：**CoPaw 运行时到底有哪些"状态"？** 经过对代码的完整分析，状态分为三个层级：

### 1.1 Per-Workspace 状态（每个 Agent 独立拥有）

| 状态 | 文件/路径 | 格式 | 重要性 | 备注 |
|------|-----------|------|--------|------|
| Agent 配置 | `{workspace}/agent.json` | JSON | 核心 | channels, MCP, system_prompt, tools, security 等 |
| 聊天列表 | `{workspace}/chats.json` | JSON | 核心 | ChatSpec 元数据（id, name, session_id 等） |
| 对话历史 | `{workspace}/sessions/*.json` | JSON | 核心 | AgentScope state_dict，可能很大 |
| 定时任务 | `{workspace}/jobs.json` | JSON | 核心 | CronJobSpec 定义 |
| 技能清单 | `{workspace}/skill.json` | JSON | 核心 | workspace-skill-manifest |
| 技能内容 | `{workspace}/skills/*/` | 目录树 | 核心 | SKILL.md + scripts + references |
| Markdown 记忆 | `{workspace}/memory/*.md` | Markdown | 核心 | AgentMdManager 管理 |
| 向量记忆 | `{workspace}/` 下 ReMe 管理的文件 | SQLite/Chroma | 核心 | 可能包含二进制文件 |
| 系统提示文件 | `{workspace}/AGENTS.md`, `SOUL.md` 等 | Markdown | 重要 | 由 agent.json 的 system_prompt_files 引用 |

### 1.2 全局状态（所有 Agent 共享）

| 状态 | 文件/路径 | 格式 | 重要性 |
|------|-----------|------|--------|
| 根配置 | `{WORKING_DIR}/config.json` | JSON | 核心 |
| UI 设置 | `{WORKING_DIR}/settings.json` | JSON | 次要 |
| 共享技能池 | `{WORKING_DIR}/skill_pool/` | 目录树+JSON | 重要 |
| Token 用量 | `{WORKING_DIR}/token_usage.json` | JSON | 次要 |
| 心跳文件 | `{WORKING_DIR}/HEARTBEAT.md` | Markdown | 次要 |
| 自定义渠道 | `{WORKING_DIR}/custom_channels/` | Python 模块 | 特殊 |

### 1.3 敏感状态（SECRET_DIR）

| 状态 | 文件/路径 | 格式 | 安全等级 |
|------|-----------|------|----------|
| Provider 配置 | `{SECRET_DIR}/providers/builtin/*.json` | JSON | 高（含 API Key） |
| 自定义 Provider | `{SECRET_DIR}/providers/custom/*.json` | JSON | 高（含 API Key） |
| 环境变量 | `{SECRET_DIR}/envs.json` | JSON | 高（可能含密钥） |
| 认证配置 | `{SECRET_DIR}/auth.json` | JSON | 高（含 JWT secret, 密码哈希） |

### 1.4 纯运行时状态（不持久化，无需 snapshot）

- 内存中的 ReMe 缓存（未 flush 的部分）
- CronManager 的 scheduler 和 job state
- TaskTracker 的活跃任务
- LLM 正在进行的流式响应
- WebSocket 连接状态
- Channel 的在线连接（DingTalk/Discord bot session 等）
- 技能环境变量覆盖的引用计数

---

## 二、核心设计决策

### 2.1 Snapshot 粒度：Per-Workspace 为主，全局可选

**决策：** Snapshot 的基本单位是 **单个 Workspace（Agent）**，同时支持可选附带全局状态。

**理由：**

- 用户最常见的需求是备份/迁移某个特定 Agent
- 全局状态（config.json）中对其他 Agent 的引用在跨设备时大概率无意义
- Per-workspace 粒度让导出文件更小、恢复更灵活

**具体方案：**

- `snapshot create <agent_id>` - 默认只快照该 workspace
- `--include-secrets` 标志 - 可选包含该 Agent 相关的 provider 配置
- `--include-global` 标志 - 可选包含全局 config + skill_pool
- `snapshot create --all` - 快照整个系统（所有 workspace + 全局 + secrets）

### 2.2 Snapshot 存储格式

**决策：** 使用 **ZIP 归档 + manifest.json 清单文件**。

```
copaw-snapshot-{agent_id}-{timestamp}.zip
  manifest.json          # 元数据、版本、校验和
  workspace/             # workspace 目录树的完整副本
    agent.json
    chats.json
    sessions/
    jobs.json
    skill.json
    skills/
    memory/
    ...
  secrets/               # 可选，加密存储
    providers/
    envs.json
  global/                # 可选
    config.json
    settings.json
    skill_pool/
```

**manifest.json 结构：**

```json
{
  "schema_version": 1,
  "copaw_version": "0.x.y",
  "agentscope_version": "0.x.y",
  "created_at": "2026-04-08T12:00:00Z",
  "agent_id": "default",
  "original_workspace_dir": "/home/user/.copaw/workspaces/default",
  "original_platform": "linux",
  "python_version": "3.11.5",
  "trust_level": "local",
  "includes_secrets": false,
  "includes_global": false,
  "file_checksums": {
    "workspace/agent.json": "sha256:...",
    "workspace/chats.json": "sha256:..."
  },
  "reme_backend": "local",
  "notes": "user-provided description"
}
```

**字段说明：**
- `agentscope_version`: 记录 AgentScope 库版本，用于判断 `sessions/*.json` 兼容性
- `trust_level`: `"local"`（本机创建）或 `"imported"`（外部导入），影响 skills 的默认启用状态
- `file_checksums`: 用于检测传输/存储过程中的意外损坏（防损坏，不防篡改）

### 2.3 敏感数据处理策略

CoPaw 中的"快照"分为两种不同的产物，敏感数据策略也不同：

**本地快照（Local Snapshot）：**
- 存储路径：`{WORKING_DIR}/snapshots/`，与 `SECRET_DIR` 在同一台机器上
- **默认包含 secrets**，因为本地恢复需要完整的 API Key / Token 才能正常运行
- 文件权限：Linux/macOS 下设置 `0600`；Windows 下通过 `os` 模块设置仅当前用户可读写的 ACL
- **注意：** 本地快照不建议上传到网盘（OneDrive/iCloud/Google Drive）或通过 IM 发送。如需跨设备传输，请使用"导出"功能

**导出包（Export Package）：**
- 用户主动导出的 ZIP 文件，会离开本机
- **默认剥离 secrets**：`agent.json` 中的 `client_secret`、channel token 等字段替换为占位符 `"<REDACTED>"`
- 使用 `--include-secrets` 时要求用户输入 `YES` 确认（而非 `y/n`），并在文件名中标记 `[CONTAINS-SECRETS]`
- `--encrypt` 标志启用加密导出，可安全传输含 secrets 的导出包

**加密方案选型：**
- 使用 Python `cryptography` 库
- 算法：AES-256-GCM（AEAD，自带认证标签）或 Fernet（AES-128-CBC + HMAC-SHA256）
- KDF：PBKDF2-HMAC-SHA256，迭代次数 >= 600,000
- 加密包格式：版本头（1 byte）+ 随机盐（16 bytes）+ 密文（含 GCM tag）
- 密码错误时通过 GCM tag / HMAC 验证失败检测，返回明确的"密码错误"提示（区分于"文件损坏"）

---

## 三、关键操作流程

### 3.1 创建 Snapshot

```
用户请求 -> 验证 agent 存在 -> 暂停写入 -> 收集文件 -> 计算校验和 -> 打包 -> 恢复写入
```

**暂停写入的必要性：** Workspace 运行时会持续写入 sessions、memory 等文件。如果在写入过程中拷贝文件，可能得到不一致的快照。

**实现方式：**

- 对 `AgentRunner` 加一个 `snapshot_lock`（读写锁）
- 正常请求持有读锁，snapshot 持有写锁
- 写锁获取后，等待当前正在进行的请求完成（有超时）
- ReMe memory 调用 flush/sync 确保内存数据落盘

**锁覆盖的入口点：**

| 入口点 | 锁行为 |
|--------|--------|
| `AgentRunner.run()` (聊天请求) | 持有读锁 |
| `CronManager` 任务执行 | 持有读锁 |
| `Workspace.reload()` | 持有写锁（与 snapshot 互斥） |
| `SnapshotManager.create()` | 持有写锁 |
| `SnapshotManager.restore()` | 直接调用 `workspace.stop()`，不需要读写锁（workspace 停止后没有竞争） |
| Channel 消息接收 | 不加锁；消息进入 runner 时才触碰读锁 |
| MCP 回调 | 属于 runner 请求的一部分，已在读锁范围内 |

Snapshot 持有写锁期间，新的聊天请求在 `runner.run()` 入口处排队等待（不丢弃），等待超时后返回"系统正在维护"的错误响应。

### 3.2 恢复 Snapshot（三阶段状态机）

恢复操作采用三阶段状态机设计，确保任意阶段崩溃后都可恢复到确定状态：

```
PHASE 1: PREPARE（准备）
  - 校验 manifest 和 file_checksums
  - 检查磁盘空间（需要 2x snapshot 大小 + 当前 workspace 大小）
  - 解压 snapshot 到 {WORKING_DIR}/_restore_staging/{agent_id}/
  - 写入状态文件 .restore_state = "prepared"

PHASE 2: APPLY（替换）
  - 调用 workspace.stop(final=True) 关闭所有服务、释放文件句柄
  - 将当前 workspace 重命名为 {workspace_dir}.backup.{timestamp}
  - 将 staging 目录重命名为 workspace_dir
  - 写入状态文件 .restore_state = "applied"

PHASE 3: VERIFY（验证）
  - 启动 workspace (workspace.start())
  - 验证核心服务可用
  - 成功：删除 .restore_state，保留 backup 目录（用户可手动清理或由 prune 清理）
  - 失败：回滚到 backup 目录，重启
```

**崩溃恢复（启动时自动检查）：**
- 发现 `.restore_state = "prepared"`：staging 目录存在但未 apply，清理 staging，正常启动原 workspace
- 发现 `.restore_state = "applied"`：apply 完成但未 verify，尝试启动新 workspace；失败则回滚到 backup
- 发现 `{workspace_dir}.backup.*` 但无 `.restore_state`：正常状态，backup 是历史遗留，可被 prune 清理

### 3.3 导出/导入

```
导出: 创建 snapshot -> 敏感数据处理 -> 可选加密 -> 输出 zip 文件
导入: 接收 zip -> 可选解密 -> 校验 manifest -> 资源限制检查 -> 版本兼容检查
      -> 创建新 workspace 或覆盖 -> 可运行性检查
```

**导入两阶段反馈：**

```
阶段 1/2: 文件导入完成
  - agent.json ✓
  - 3 个技能已导入（已禁用，待审核）
  - 12 个对话历史已导入
  - 定时任务已导入（已暂停）

阶段 2/2: 可运行性检查
  - Provider "openai": API Key 未配置 ⚠️
  - Channel "dingtalk": 需要重新认证 ⚠️
  - MCP "filesystem": 连接正常 ✓
  - 技能依赖 "requests>=2.28": 已安装 ✓
  - 技能依赖 "pandas": 未安装 ⚠️ (pip install pandas)
```

---

## 四、边缘情况与用户问题全面分析

### 4.1 一致性问题

| 场景 | 风险 | 应对策略 |
|------|------|----------|
| Snapshot 期间有活跃的聊天流式响应 | Session 文件半写入，状态不一致 | 获取写锁时等待进行中请求完成，设 30s 超时；超时则中断流式响应并通知用户 |
| Snapshot 期间 CronJob 正在执行 | 类似活跃聊天 | CronManager 的任务也需要纳入锁范围 |
| ReMe 内存数据未 flush 到磁盘 | 恢复后丢失最近的记忆 | Snapshot 前显式调用 memory_manager 的 flush/save |
| Channel 正在接收消息 | 消息排队延迟 | Channel 层不暂停，只暂停 runner 处理；写锁期间新请求在 runner 入口排队等待，超时后返回"系统维护中"错误响应 |

### 4.2 恢复失败场景

| 场景 | 风险 | 应对策略 |
|------|------|----------|
| 恢复过程中断电/崩溃 | workspace 处于半恢复状态 | 三阶段状态机（见 3.2 节）：崩溃后启动时自动检测 `.restore_state` 并恢复到确定状态 |
| 磁盘空间不足 | 解压/拷贝中途失败 | PREPARE 阶段预检可用空间（需要 2x snapshot 大小 + 当前 workspace 大小）；失败时清理 staging 目录 |
| 文件权限问题 | Windows/Linux 权限模型不同 | 忽略原始权限，使用当前平台的默认权限；Windows 下通过 `os` 模块设置 ACL（而非 POSIX `0600`） |
| Snapshot 文件损坏 | 解压失败或文件不完整 | PREPARE 阶段校验 manifest 中的 SHA-256 校验和 + ZIP 自身 CRC；校验失败则中止恢复 |
| Windows 文件占用 | 目录重命名时文件被占用 | APPLY 阶段先调用 `workspace.stop(final=True)` 释放所有文件句柄后再重命名 |

### 4.3 跨设备/跨平台迁移

| 场景 | 风险 | 应对策略 |
|------|------|----------|
| Windows -> Linux 路径差异 | agent.json 中可能有绝对路径引用 | 导入时扫描并自动转换路径分隔符；manifest 记录原始平台 |
| 不同 CoPaw 版本间迁移 | 配置 schema 不兼容 | manifest 中记录 copaw_version；导入时运行 migration 检查 |
| Agent ID 冲突 | 目标设备已有同名 agent | 默认选项为重命名（如 `default` -> `default-imported-20260408`）；覆盖需要 CLI `--force` 或 UI 二次确认（输入 agent 名称）；也可取消 |
| AgentScope 版本不匹配 | session 文件反序列化失败 | 不兼容的 session 文件不阻塞导入；首次打开对应聊天时捕获错误，提示"此对话历史与当前版本不兼容，已归档" |
| ReMe 后端不一致 | 源用 Chroma，目标只有 local | manifest 记录 reme_backend；导入时检查目标环境是否支持 |
| Python 版本差异 | pickle/SQLite 二进制兼容性 | manifest 记录 python_version；小版本差异允许，大版本差异警告 |
| 技能引用的外部依赖缺失 | pip 包目标设备未安装 | 导入后自动检查 skill requirements，提示用户安装缺失依赖 |

### 4.4 安全相关

| 场景 | 风险 | 应对策略 |
|------|------|----------|
| 用户误导出含 API Key 的快照并分享 | 密钥泄露 | 默认不含 secrets；`--include-secrets` 需输入 `YES` 确认；文件名标记 `[CONTAINS-SECRETS]`；默认推荐加密导出 |
| 加密快照密码遗忘 | 数据无法恢复 | 通过 GCM tag/HMAC 验证区分"密码错误"和"文件损坏"两种情况；不提供密码找回 |
| 导入不受信任的快照 | 恶意技能代码 | 导入的 skills 在 `skill.json` 中设置 `enabled: false`，需用户逐个审核后手动启用 |
| 导入不受信任的快照 | 路径遍历攻击 | ZIP 解压时验证所有路径不含 `..` 或绝对路径（防 zip slip）；符号链接和硬链接一律跳过 |
| auth.json 中的密码哈希 | 导入后认证可能失效 | 导入时 auth.json 默认不覆盖目标设备的现有认证配置 |
| 导入资源炸弹 | zip bomb 占满磁盘 | 导入时强制执行资源限制（见 4.7 节） |

### 4.5 用户体验陷阱

| 场景 | 风险 | 应对策略 |
|------|------|----------|
| Snapshot 文件过大 | 导出/传输耗时，用户以为卡死 | 显示进度条和预估剩余时间；支持 `--exclude-sessions` 和 `--exclude-memory` 选项；创建前预估大小并提示；支持取消操作 |
| 恢复后 Channel 需要重新认证 | OAuth token 过期或与设备绑定 | 导入两阶段反馈的"可运行性检查"中自动检测 channel 状态，生成"需要重新配置"的提示列表 |
| 恢复后 MCP Server 连接失败 | 目标设备没有对应的 MCP server | 优雅降级：MCP 连接失败不阻塞启动，在 UI 中标记不可用 |
| 用户以为恢复等于"撤销" | 不会影响外部系统 | CLI/UI 恢复确认时显示："恢复快照仅回滚 CoPaw 本地配置和数据。已通过渠道发送的消息、已执行的定时任务、已调用的第三方 API 不会被撤销。确认继续？" |
| 多次快照管理混乱 | 不记得哪个快照对应什么 | 强制/建议添加描述；列表展示时间、大小、描述、包含内容摘要 |
| 用户重复点击导入/导出 | 并发操作与状态竞争 | 同一 agent 同时只允许一个 snapshot/restore 操作（互斥锁），重复请求返回 409 Conflict 和当前任务进度 |
| 恢复前备份占用磁盘 | `.backup.*` 目录积累 | 提供 `snapshot prune` 命令清理历史备份 |

### 4.6 并发与竞态

| 场景 | 风险 | 应对策略 |
|------|------|----------|
| 同时创建两个 snapshot | 文件锁冲突 | 使用互斥锁，第二个请求返回 409 Conflict："正在创建快照，请稍后" |
| Snapshot 进行中用户触发 reload_agent | workspace 状态竞争 | `snapshot_lock` 也需要在 reload 路径中获取写锁，两者互斥 |
| 恢复进行中有新消息进入 | runner 已停止 | APPLY 阶段前已调用 `workspace.stop()`，channel 连接已断开；VERIFY 阶段重启后恢复正常 |

### 4.7 导入资源限制

防止恶意或异常的导入包消耗过多系统资源：

| 维度 | 默认限制 | 可配置 |
|------|----------|--------|
| 解压后总大小 | 2 GB | 是（通过 config） |
| 单文件大小 | 500 MB | 是 |
| 文件数量 | 10,000 | 是 |
| 最大路径深度 | 20 级 | 否 |
| 最大路径长度 | 260 字符（兼容 Windows） | 否 |
| 压缩比阈值 | 解压大小 / 压缩大小 > 100 时拒绝 | 否 |

解压过程中实时统计，超出任一限制立即中止并清理临时文件。符号链接和硬链接在解压时一律跳过。

---

## 五、API 设计

### 5.1 REST API

```
POST   /api/agents/{agentId}/snapshots              # 创建快照
GET    /api/agents/{agentId}/snapshots              # 列出快照
GET    /api/agents/{agentId}/snapshots/{id}         # 获取快照详情
DELETE /api/agents/{agentId}/snapshots/{id}         # 删除快照
POST   /api/agents/{agentId}/snapshots/{id}/restore # 恢复快照
GET    /api/agents/{agentId}/snapshots/{id}/export  # 导出（下载 zip）
POST   /api/agents/{agentId}/snapshots/import       # 导入（上传 zip）
POST   /api/snapshots                               # 全局快照（所有 agent）
```

### 5.2 CLI

```bash
copaw snapshot create [agent_id] [--note "..."] [--include-secrets] [--include-global]
copaw snapshot list [agent_id]
copaw snapshot restore <snapshot_id> [--target-agent <new_id>]
copaw snapshot export <snapshot_id> [--output <path>] [--encrypt]
copaw snapshot import <file_path> [--agent-id <id>] [--force]
copaw snapshot delete <snapshot_id>
copaw snapshot prune [--keep <N>]
```

---

## 六、实现架构

### 关键模块职责

- **SnapshotManager** (`src/copaw/app/snapshot/manager.py`): 顶层协调器，管理快照生命周期
- **StateCollector** (`src/copaw/app/snapshot/collector.py`): 收集 workspace 文件，处理 flush 和锁
- **SnapshotPacker** (`src/copaw/app/snapshot/packer.py`): 打包/解包 ZIP，生成 manifest，计算校验和
- **SnapshotRestorer** (`src/copaw/app/snapshot/restorer.py`): 恢复逻辑，包括备份、替换、重启
- **SecretSanitizer** (`src/copaw/app/snapshot/sanitizer.py`): 剥离/加密敏感信息

---

## 七、已确认的设计决策

1. **自动快照** - 不做自动快照，全部由用户手动触发
2. **增量快照** - 第一版不做，每次全量快照（ZIP 打包完整 workspace）
3. **快照大小** - 不限制大小，但创建前预估并提示用户确认；提供 `--exclude-sessions` / `--exclude-memory` 排除选项减小体积
4. **ReMe 向量存储可移植性** - 同时保留二进制文件和纯文本记忆导出；导入时优先使用二进制，不兼容时回退到纯文本重建索引
5. **Console UI** - 第一版就做完整 UI（列表、创建、恢复、导入导出、进度条等）
