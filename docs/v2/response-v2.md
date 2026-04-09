# Snapshot 设计二轮 Review 回复

针对 `docs/review-v2.md` 的逐条回复。

---

## P0 阻塞项回复

### P0-1: `trust_level` 不能作为安全决策输入 -- 完全采纳

**Reviewer 分析完全正确。**

当前设计让 `trust_level` 存在于 manifest 中，同时用它影响 skills 的默认启用状态。但 manifest 是打包在 ZIP 里的，攻击者可以直接把它改成 `"local"` 来绕过 skills 禁用逻辑。这是一个真实的安全漏洞。

**修改方案：**

- 从 manifest 中移除 `trust_level` 字段，改为 `source_hint`（仅作展示用途，不参与任何安全决策）
- 信任判定改为**由导入路径决定**，而非由包内容声明：
  - **本地恢复路径**（`SnapshotManager.restore(snapshot_id)`，从 `{WORKING_DIR}/snapshots/` 读取）：视为 trusted
  - **外部导入路径**（`SnapshotManager.import_snapshot(file_path)`，用户上传或指定的 ZIP）：一律视为 untrusted
- 代码层面：`import_snapshot()` 函数硬编码 `trusted=False`，不读取 manifest 中的任何字段来覆盖

---

### P0-2: 恢复状态机的状态落盘位置和崩溃边界仍不可靠 -- 完全采纳

**Reviewer 分析完全正确。** 把 `.restore_state` 放在 staging 目录里，而 staging 目录自身会被 rename，这导致状态文件在关键时刻不可达。

更具体地说，APPLY 阶段有三个步骤：

```
A1: current workspace_dir -> workspace_dir.backup.{ts}
A2: staging_dir -> workspace_dir
A3: 写入 .restore_state = "applied"
```

如果 A1 成功、A2 失败，原 `workspace_dir` 已不存在，但 staging 还在。当前设计的崩溃恢复分支没有覆盖这种情况。

如果 A2 成功、A3 失败，`.restore_state` 已随 staging 变成了新的 `workspace_dir`，但内容仍然是 `"prepared"`，崩溃恢复会误判为"staging 存在但未 apply，清理 staging"，这会错误地删掉刚恢复好的 workspace。

**修改方案：**

状态文件改为稳定路径 `{WORKING_DIR}/_restore_state/{agent_id}.json`，记录完整上下文：

```json
{
  "phase": "applying",
  "agent_id": "default",
  "workspace_dir": "/home/user/.copaw/workspaces/default",
  "backup_dir": "/home/user/.copaw/workspaces/default.backup.20260408120000",
  "staging_dir": "/home/user/.copaw/_restore_staging/default",
  "snapshot_id": "snap-abc123",
  "started_at": "2026-04-08T12:00:00Z",
  "last_completed_step": "workspace_renamed_to_backup"
}
```

状态机细化为以下步骤，每完成一个原子步骤就更新 `last_completed_step`：

```
PHASE 1: PREPARE
  step: "checksum_verified"     -> 校验完成
  step: "staging_extracted"     -> 解压到 staging 完成
  phase 更新为 "prepared"

PHASE 2: APPLY
  step: "workspace_stopped"     -> workspace.stop() 完成
  step: "workspace_renamed_to_backup" -> 原目录重命名为 backup 完成
  step: "staging_renamed_to_workspace" -> staging 重命名为 workspace 完成
  phase 更新为 "applied"

PHASE 3: VERIFY
  step: "workspace_started"     -> 新 workspace 启动完成
  step: "services_verified"     -> 核心服务验证通过
  -> 删除状态文件，恢复完成
```

**崩溃恢复逻辑（启动时扫描 `_restore_state/` 目录）：**

| `last_completed_step` | 现场状态 | 恢复动作 |
|---|---|---|
| `staging_extracted` 或更早 | staging 存在，原 workspace 完好 | 清理 staging，正常启动 |
| `workspace_stopped` | workspace 已停止但目录仍在，staging 存在 | 正常启动原 workspace |
| `workspace_renamed_to_backup` | 原目录已变成 backup，staging 仍存在，workspace_dir 不存在 | 将 staging 重命名为 workspace_dir，进入 VERIFY |
| `staging_renamed_to_workspace` | 新 workspace 就位，backup 存在 | 进入 VERIFY |
| `workspace_started` | 新 workspace 已启动但未验证 | 重新验证，失败则回滚到 backup |

---

### P0-3: 并发模型没有接住现有 `reload_agent()` 的真实行为 -- 完全采纳

**Reviewer 分析完全正确。** 这是本轮 review 中最有价值的一条。

阅读 `MultiAgentManager.reload_agent()` 的实际代码后确认：

1. reload 在锁外创建并启动新 Workspace（指向同一个 `workspace_dir`）
2. 只在 swap 时短暂持锁
3. 旧实例的停止是异步延迟的（等待 active tasks）
4. `schedule_agent_reload()` 通过 `asyncio.create_task()` 在后台触发，至少有 15+ 处调用点（config/channel/MCP/skill/tool/provider 变更都会触发）

这意味着 `snapshot_lock`（workspace 内部锁）完全无法阻止以下竞态：

- restore 正在 APPLY 阶段重命名目录，reload 同时在锁外读同一个 `workspace_dir` 创建新实例
- snapshot create 正在持写锁收集文件，reload 已经创建了新实例并准备 swap，旧实例的锁对新实例无效
- 两个 `schedule_agent_reload()` 在快照操作前后各触发一次，产生交错

**修改方案：**

在 `MultiAgentManager` 中引入 **per-agent lifecycle lock**（操作锁），所有改变 agent 生命周期的操作必须串行化：

```python
class MultiAgentManager:
    def __init__(self):
        self.agents: Dict[str, Workspace] = {}
        self._lock = asyncio.Lock()  # 现有的 dict 访问锁
        self._agent_op_locks: Dict[str, asyncio.Lock] = {}  # 新增：per-agent 操作锁

    def _get_agent_op_lock(self, agent_id: str) -> asyncio.Lock:
        if agent_id not in self._agent_op_locks:
            self._agent_op_locks[agent_id] = asyncio.Lock()
        return self._agent_op_locks[agent_id]
```

以下操作必须持有 per-agent op lock（同一 agent 内串行，不同 agent 之间可并行）：

| 操作 | 锁行为 |
|------|--------|
| `reload_agent(agent_id)` | 持有 `_agent_op_locks[agent_id]` |
| `snapshot_create(agent_id)` | 持有 `_agent_op_locks[agent_id]` |
| `snapshot_restore(agent_id)` | 持有 `_agent_op_locks[agent_id]` |
| `snapshot_import(agent_id)` | 持有 `_agent_op_locks[agent_id]` |
| `snapshot_export(agent_id)` | 持有 `_agent_op_locks[agent_id]` |
| `stop_agent(agent_id)` | 持有 `_agent_op_locks[agent_id]` |

**`schedule_agent_reload()` 在 snapshot/restore 期间的行为：**

- `_agent_op_locks[agent_id]` 使用 `asyncio.Lock()`，非可重入
- snapshot/restore 持锁期间，`schedule_agent_reload()` 触发的 `reload_agent()` 会在 `async with op_lock` 处排队等待
- snapshot/restore 完成后，排队的 reload 自然执行
- 这是**排队语义**（而非拒绝或取消），因为 reload 通常是配置变更后的刷新操作，skip 可能导致配置不一致

**对原有 `snapshot_lock`（workspace 内部读写锁）的处理：**

原来设计的 workspace 内部读写锁仍然保留，但职责收窄为：
- 读锁：保护 `AgentRunner.run()` 和 `CronManager` 任务执行期间的文件一致性
- 写锁：snapshot create 期间暂停新的请求处理、flush 内存

两层锁的关系：**op lock 是外层粗粒度锁（串行化生命周期操作），snapshot_lock 是内层细粒度锁（协调请求处理和文件收集）。**

---

### P0-4: 第三方导入的隔离范围仍然太窄 -- 完全采纳

**Reviewer 分析正确。** 只禁用 skills 是不够的。`jobs.json` 中的 cron 任务如果立即执行，同样会产生不可控的外部副作用。channel 自动连接、MCP 自动连接也是类似的风险。

**修改方案 - 定义"导入后静默态"（Post-Import Quiescent State）：**

对 untrusted 导入包，以下组件在导入后默认处于非活跃状态：

| 组件 | 导入后默认状态 | 用户激活方式 |
|------|---------------|-------------|
| Skills | `enabled: false`（在 `skill.json` 中） | 逐个审核后手动启用 |
| Cron Jobs | `enabled: false`（在 `jobs.json` 中） | 审核后手动启用 |
| Channels | 不自动连接（`agent.json` 中标记 `_imported_disabled: true`） | 重新认证后启用 |
| MCP Clients | 不自动连接 | 用户确认后手动连接 |
| Agent 核心（Runner） | 正常启动 | 即时可用（通过 Console 聊天） |

**理由：** Runner 本身需要启动，否则用户无法通过 Console 与 Agent 交互来验证导入是否正确。被禁用的是"主动对外动作"的能力（定时任务、渠道推送、MCP 调用），而非 Agent 的基本对话能力。

**技术实现：** 导入流程中增加一个 `quarantine_imported_config()` 步骤，在写入 workspace 前修改配置：

- 遍历 `jobs.json`，将所有 job 的 `enabled` 设为 `false`
- 在 `agent.json` 的 channels 配置中添加 `_imported_disabled: true` 标记
- 在 `agent.json` 的 MCP 配置中添加 `_imported_disabled: true` 标记
- skills 的 `enabled: false` 已有（上一轮采纳）

---

## P1 设计问题回复

### P1-1: "密码错误"和"文件损坏"不能被可靠区分 -- 完全采纳

Reviewer 说得对。AES-256-GCM 的 authentication tag 验证失败只说明"密钥不对或密文被修改"，无法精确区分这两种情况。

**修改方案：**

错误提示改为：`"解密失败：密码错误或文件已损坏/被篡改"`

不再承诺精确区分。

---

### P1-2: 加密方案没有收敛到单一实现 -- 完全采纳

Reviewer 正确，V1 同时支持两套加密格式是不必要的复杂度。

**修改方案：**

V1 固定使用 **AES-256-GCM** 一种方案，不使用 Fernet：

```
加密包格式（固定）：
  version: 1 byte (0x01)
  salt:    16 bytes (random)
  nonce:   12 bytes (random)
  ciphertext: variable (含 16 bytes GCM tag at end)

KDF: PBKDF2-HMAC-SHA256, iterations >= 600,000
Key: 256-bit derived from password + salt
```

Fernet 从文档中移除。未来如需支持新算法，通过 version byte 区分。

---

### P1-3: 长任务互斥域定义仍然不一致 -- 完全采纳

Reviewer 正确，export 和 import 本身也是长任务，也涉及 staging 目录和磁盘 I/O，应纳入互斥范围。

**修改方案：**

这一点已被 P0-3 的 per-agent lifecycle lock 完全覆盖。op lock 的覆盖范围包括：create / restore / export / import / reload，同一 agent 的这五种操作串行执行，API 层面重复请求返回 409 Conflict。

---

## V1 功能闭环问题回复

### V1-1: `restore` 的产品语义还不清楚 -- 部分采纳

Reviewer 正确指出"原地覆盖"和"克隆到新 agent"是两种不同的用户任务。

**采纳部分：**

在文档中明确拆分两种模式：

| 模式 | 命令 | 行为 |
|------|------|------|
| 原地恢复（rollback） | `copaw snapshot restore <id>` | 覆盖当前 workspace，需确认 |
| 克隆试跑（clone） | `copaw snapshot restore <id> --as <new_agent_id>` | 创建新 agent，不影响现有 |

将 `--target-agent` 改为更直观的 `--as`。

**不采纳部分：**

不同意"默认是恢复为新 agent"。理由：

- "restore" 这个词的用户心智模型就是"回到之前的状态"，即原地恢复
- 如果默认行为是克隆，用户会困惑"我的 agent 怎么没变回去"
- 原地恢复需要确认（CLI 要求输入 `YES`，UI 弹出确认对话框），这已经足以防误操作
- 克隆功能通过显式的 `--as` 标志提供，不会被意外触发

---

### V1-2: 导入完成后 agent 处于什么状态，没有定义清楚 -- 采纳

Reviewer 正确，用户最关心的是"现在能不能用"。

**修改方案：**

结合 P0-4 的"导入后静默态"，定义导入后 agent 的三种显式状态：

| 状态 | 含义 | UI 展示 |
|------|------|---------|
| `needs_setup` | 缺少必要配置（如 provider API Key），agent 无法正常对话 | 红色标签 + 修复向导入口 |
| `needs_review` | 可以对话，但有组件待审核/启用（skills, jobs, channels） | 黄色标签 + 待办清单入口 |
| `ready` | 所有组件就绪，功能完整 | 绿色标签 |

状态由可运行性检查自动判定：
- 缺少 provider -> `needs_setup`
- 有 disabled skills/jobs/channels -> `needs_review`
- 全部正常 -> `ready`

---

### V1-3: 第三方导入后功能过早生效 -- 已在 P0-4 中完全覆盖

---

### V1-4: 三个概念容易混淆 -- 采纳

**修改方案 - 补充操作对照表：**

| | 本地快照 | 导出包 | 导入包 |
|------|---------|--------|--------|
| **目的** | 本机回滚/备份 | 跨设备迁移或分享 | 接收外部 agent |
| **创建命令** | `snapshot create` | `snapshot export` | N/A |
| **使用命令** | `snapshot restore` | N/A (发送给对方) | `snapshot import` |
| **默认含 secrets** | 是 | 否 | N/A |
| **适合分享** | 否 (含 secrets) | 是 | N/A |
| **skills 状态** | 保持原样 | 保持原样 | 默认禁用 |
| **jobs/channels** | 保持原样 | 保持原样 | 默认禁用 |
| **风险提示** | 不要上传到网盘/IM | 确认是否含 secrets | 审核后再启用组件 |

---

### V1-5: 全局快照不适合在 V1 做恢复 -- 采纳

Reviewer 正确。全局恢复涉及多 agent 配置冲突、`config.json` 合并策略等复杂问题。

**修改方案：**

- V1 保留 `snapshot create --all`（创建全局快照）
- V1 **不支持**全局恢复（`snapshot restore --all`）
- 全局快照只能按单个 agent 逐一导入：用户解压全局快照后，选择需要的 agent 分别导入
- 在 CLI 和 API 中，全局恢复入口直接返回 `"全局恢复将在未来版本支持。当前请使用 snapshot import 逐个导入 agent。"`

---

### V1-6: 历史会话的不兼容提示暴露得太晚 -- 采纳

Reviewer 正确。导入时"一切正常"，打开聊天时才发现坏了，用户会认为是 bug 而非兼容性问题。

**修改方案：**

导入阶段增加 session 预扫描：

1. 遍历 `sessions/*.json`，尝试解析 JSON 结构（不需要完整反序列化 AgentScope state_dict，只需验证 JSON 合法性 + 检查关键版本标记）
2. 对比 manifest 中的 `agentscope_version` 与当前环境的 AgentScope 版本
3. 在导入阶段 1 的反馈中汇总：

```
阶段 1/2: 文件导入完成
  - agent.json ✓
  - 3 个技能已导入（已禁用，待审核）
  - 2 个定时任务已导入（已暂停）
  - 对话历史: 10 个可用, 2 个不兼容（已归档）
    原因: AgentScope 版本不匹配 (快照: 0.8.1, 当前: 0.9.0)
```

---

### V1-7: 缺少"导入后待办清单" -- 采纳

Reviewer 正确。光展示问题不够，用户需要知道"下一步去哪修"。

**修改方案：**

导入完成后生成有序待办清单，包含具体的修复命令/入口：

**CLI 输出：**

```
导入完成。以下是让 Agent 完全就绪的待办事项：

 1. [必须] 配置 Provider API Key
    -> copaw config provider set openai --api-key <YOUR_KEY>
    -> 或在 Console 设置页面配置

 2. [必须] 重新认证 Channel
    -> Console -> Channels -> dingtalk -> 重新配置

 3. [建议] 安装技能依赖
    -> pip install pandas

 4. [建议] 审核并启用技能 (3 个待审核)
    -> copaw skill list --status disabled
    -> copaw skill enable <skill_name>

 5. [建议] 审核并启用定时任务 (2 个已暂停)
    -> copaw job list --status paused
    -> copaw job enable <job_id>
```

**Console UI：**

- 导入后 agent 卡片显示状态标签（`needs_setup` / `needs_review`）
- 点击进入修复向导页面，按优先级列出待办事项，每项带"修复"按钮跳转到对应设置页

---

## 建议的 V1 范围回复

Reviewer 建议的四条闭环优先级完全同意：

1. 本地快照创建 + 原地恢复 ✓
2. 导出包 + 导入到新 agent ✓
3. 导入后的统一隔离状态（静默态） ✓
4. 导入完成后的 checklist / 修复入口 ✓

**关于建议延期的内容：**

| 项目 | 判定 | 理由 |
|------|------|------|
| 全局恢复 | 同意延期 | V1 只支持全局创建，不支持全局恢复 |
| 跨平台路径自动修复 | 同意降级 | V1 只做检测和报告，不做自动修复 |
| 多种加密格式 | 同意 | V1 固定 AES-256-GCM，已在 P1-2 中收敛 |

---

## 补充测试用例回复

所有建议的测试用例采纳，纳入测试计划。

### P0 测试用例确认

- **trust_level 伪造**：篡改 manifest 的 `source_hint`（原 `trust_level`），验证导入后 skills/jobs/channels 仍然处于静默态。由于 trust 判定已改为由代码路径决定，manifest 中不存在可被利用的字段。
- **恢复状态机崩溃边界**：在 `_restore_state/{agent_id}.json` 的每个 `last_completed_step` 值处模拟崩溃，验证启动时恢复逻辑覆盖所有分支。
- **restore + reload 竞态**：restore 进行中通过 API 修改配置触发 `schedule_agent_reload()`，验证 reload 在 op lock 处排队等待，不会出现双实例。

### P1 测试用例确认

- **untrusted 包包含 jobs/channels/MCP**：验证导入后全部处于静默态，不会主动执行外部操作。
- **加密错误场景**：密码错误、密文损坏、version byte 不支持三种情况，验证统一的错误提示和错误码。
- **操作互斥**：连续触发 import + export + create + restore，验证 op lock 串行化 + 409 Conflict 返回。

---

## 总结

| Review 编号 | 级别 | 判定 | 要点 |
|-------------|------|------|------|
| P0-1 trust_level | P0 | 完全采纳 | 从 manifest 移除，改为代码路径决定信任 |
| P0-2 状态机落盘 | P0 | 完全采纳 | 状态文件移到稳定路径，细化步骤追踪 |
| P0-3 并发模型 | P0 | 完全采纳 | 引入 per-agent lifecycle lock 在 manager 层 |
| P0-4 隔离范围 | P0 | 完全采纳 | 定义"导入后静默态"，skills/jobs/channels/MCP 全部默认禁用 |
| P1-1 密码错误提示 | P1 | 完全采纳 | 改为"密码错误或文件已损坏/被篡改" |
| P1-2 加密方案收敛 | P1 | 完全采纳 | V1 固定 AES-256-GCM |
| P1-3 互斥域范围 | P1 | 完全采纳 | 已被 P0-3 的 op lock 覆盖 |
| V1-1 restore 语义 | V1 | 部分采纳 | 拆分原地/克隆两种模式，但默认仍为原地恢复 |
| V1-2 导入后状态 | V1 | 采纳 | 定义 needs_setup / needs_review / ready 三态 |
| V1-3 功能过早生效 | V1 | 已覆盖 | 见 P0-4 |
| V1-4 概念混淆 | V1 | 采纳 | 补充操作对照表 |
| V1-5 全局恢复 | V1 | 采纳 | V1 只支持全局创建，不支持全局恢复 |
| V1-6 会话不兼容 | V1 | 采纳 | 导入阶段预扫描 + 汇总报告 |
| V1-7 待办清单 | V1 | 采纳 | CLI 输出有序 checklist，UI 提供修复向导 |

**本轮 4 个 P0 全部采纳并给出了具体修改方案。** 其中 P0-3（并发模型与 reload_agent 的对接）是最关键的架构变更，需要在 `MultiAgentManager` 中引入 per-agent operation lock，并将 `SnapshotManager` 的所有操作注册为 manager 层的协调点。
