# Snapshot 设计文档二轮 Review

审阅对象：
- `docs/snapshot-design.md`
- `docs/response.md`

本轮 review 的目标：
- 检查 `response.md` 中承诺采纳的点，是否已经真正落到设计文档中
- 从实现可落地性角度，重新评估安全、并发、恢复、V1 功能闭环是否已经足够清晰

## 总体结论

这版设计相比上一版有明显进步，尤其是以下几点：
- 第三方导入的 skills 默认禁用，这是真正有效的收敛措施
- 恢复流程从“原子替换”的口头描述，进步为显式状态机
- 增加了资源限制、导入两阶段反馈、Agent ID 冲突默认重命名，用户体验更成熟

但我仍然认为：**这份方案还不适合直接进入实现阶段。**

当前还有两类问题没有闭环：
- `P0` 级阻塞问题：会直接导致安全策略失效、恢复逻辑错误、并发行为打架
- `V1` 级功能问题：即使实现出来，也会让用户最常见的使用路径不够顺畅或容易误操作

## P0 阻塞项

### 1) `trust_level` 不能作为安全决策输入

问题：
- 设计文档新增了 `manifest.trust_level`，并写明它会影响 skills 的默认启用状态
- 同时文档又明确 `file_checksums` 只“防损坏，不防篡改”，且 V1 不引入签名机制

影响：
- 任何第三方导入包都可以把 `trust_level` 伪装成 `"local"`
- 如果“导入技能默认禁用”依赖这个字段，就能被直接绕过

建议：
- `trust_level` 只能由导入来源决定，不能相信导入包里的 manifest
- 从 `{WORKING_DIR}/snapshots/` 读取并恢复，才可视为 local snapshot
- 任何用户上传或选择的 zip 文件，一律视为 imported / untrusted
- manifest 里最多保留 `source_hint` 这类展示字段，不参与安全判断

### 2) 恢复状态机的状态落盘位置和崩溃边界仍不可靠

问题：
- 设计把 `.restore_state` 写在 staging 目录里
- APPLY 阶段会把 staging 重命名为 `workspace_dir`，然后再把状态从 `"prepared"` 改成 `"applied"`

影响：
- 如果 `staging -> workspace_dir` 成功后、写入 `"applied"` 前崩溃，系统读到的仍是 `"prepared"`，但现场状态已经不是 prepared
- 如果 `current workspace -> backup` 成功后、`staging -> workspace_dir` 前崩溃，原 `workspace_dir` 已不存在，文档中的恢复分支没有覆盖
- 启动恢复时会误判现场，最坏情况下把本来还能回滚的 backup 再次破坏

建议：
- 不要把状态文件放在会被 rename 的目录内部
- 改为稳定路径，例如 `{WORKING_DIR}/_restore_state/{agent_id}.json`
- 状态机至少记录：
  - `phase`
  - `workspace_dir`
  - `backup_dir`
  - `staging_dir`
  - 最近完成的原子步骤
- 启动恢复逻辑必须完整覆盖多种崩溃现场，而不是只按 `prepared/applied` 两个字面值判断

### 3) 并发模型没有接住现有 `reload_agent()` 的真实行为

问题：
- 文档把 reload 简化成“workspace 上的写锁”
- 但当前系统实际是 `MultiAgentManager.reload_agent()`，它会：
  - 在锁外创建并启动新 workspace
  - 再 swap
  - 再异步停止旧实例
- 同时，多处路由会通过 `schedule_agent_reload()` 异步触发 reload

影响：
- `snapshot_lock` 只是 workspace 内部锁，约束不到 manager 层的生命周期切换
- `restore()` 又被定义为“不需要读写锁，直接 `workspace.stop()`”
- 结果就是 restore、snapshot、后台 reload 之间仍然可能互相穿透

典型风险：
- restore 进行中，后台 reload 启动了指向同一路径的新 workspace
- snapshot 正在等待写锁时，reload 已经把新实例换上去
- old/new workspace 对同一目录交错读写，产生文件句柄冲突和回滚错位

建议：
- 引入 per-agent lifecycle lock，而不是只靠 workspace 内部锁
- 至少统一串行化这些操作：
  - load
  - reload
  - snapshot create
  - snapshot restore
  - import overwrite
- 文档必须明确：后台 `schedule_agent_reload()` 在 snapshot/restore 期间是排队、拒绝还是取消

### 4) 第三方导入的隔离范围仍然太窄

问题：
- 当前主要把风险收敛到 `skills`
- 但快照中实际还能影响行为的内容还有：
  - `agent.json`
  - `AGENTS.md` / `SOUL.md` / `PROFILE.md`
  - `jobs.json`
  - 可选 global 配置和 shared skill pool

影响：
- 即使 skills 全部 disabled，导入包仍可能通过 prompt、tool 配置、channel、MCP、jobs 改变 agent 行为
- 文档里写了“定时任务已导入（已暂停）”，但这只是示例输出，不是强制规则

建议：
- 对 imported / untrusted 包统一定义“导入后静默态”
- 最低建议：
  - skills disabled
  - jobs paused
  - channels disabled 或 `needs_reauth`
  - MCP 不自动连接
- 同时提供导入后变更摘要，让用户知道导入包修改了哪些能力边界

## P1 设计问题

### 1) “密码错误”和“文件损坏”不能被可靠区分

问题：
- 文档写到：GCM tag / HMAC 失败时返回明确的“密码错误”提示，并区分于“文件损坏”

影响：
- 对 AEAD/HMAC 而言，认证失败通常只能说明“密码不对或密文已损坏/被篡改”
- 如果统一报成“密码错误”，会误导用户和排查方向

建议：
- 文案改为：`密码错误或文件已损坏/被篡改`
- 不要在 V1 里承诺做不到的精确区分

### 2) 加密方案没有收敛到单一实现

问题：
- 文档目前写的是 `AES-256-GCM 或 Fernet`
- 同时又定义了自己的“版本头 + 盐 + 密文”封装

影响：
- V1 同时支持两套加密格式会直接增加实现复杂度和测试成本
- Fernet 自己就带 framing，和自定义封装混用很容易出现双层格式不一致

建议：
- V1 只固定一种加密格式
- 更推荐单一的 password-based AEAD 方案，把 header/version/盐/nonce/tag 一次定义清楚

### 3) 长任务互斥域定义仍然不一致

问题：
- 文档在用户体验部分写的是“用户重复点击导入/导出”
- 但应对策略写成了“同一 agent 同时只允许一个 snapshot/restore 操作”

影响：
- `import` 和 `export` 本身就是长任务，也会占用 staging、磁盘、打包和 agent 生命周期资源
- 如果互斥范围只覆盖 snapshot/restore，import/export 仍可能重入

建议：
- 明确 per-agent operation lock 的实际覆盖范围
- 至少统一覆盖：
  - create snapshot
  - restore snapshot
  - export snapshot
  - import snapshot
  - overwrite import

## V1 功能闭环问题

### 1) `restore` 的产品语义还不清楚

问题：
- CLI 存在 `copaw snapshot restore <snapshot_id> [--target-agent <new_id>]`
- 这意味着 restore 既可能是“原地覆盖恢复”，也可能是“恢复成一个新 agent”
- 但正文恢复流程主要描述的是原地覆盖

影响：
- “覆盖恢复”和“克隆试跑”是两种完全不同的用户任务
- 用户很容易在“只是想试试”的情况下误覆盖现有 agent

建议：
- 文档中明确拆分两种模式：
  - `restore in-place`
  - `restore as new agent`
- V1 更建议默认是“恢复为新 agent”，原地覆盖必须显式确认

### 2) 导入完成后 agent 处于什么状态，没有定义清楚

问题：
- 文档新增了“可运行性检查”
- 但没有定义导入完成后 agent 的最终状态

影响：
- 用户最关心的是“现在能不能用”，而不是“检查做完没有”
- 如果缺 secrets、依赖、认证或 MCP 连接，agent 仍自动开放服务，体验会非常差

建议：
- 定义导入后的显式状态，例如：
  - `imported_blocked`
  - `imported_needs_review`
  - `imported_ready`
- UI/CLI 直接展示状态，而不是只展示检查项

### 3) 第三方导入后，很多功能可能过早生效

问题：
- imported skills 默认 disabled 是对的
- 但下列内容是否默认生效，文档仍未明确：
  - `jobs.json`
  - channels
  - MCP
  - `agent.json` 中的工具和安全配置

影响：
- 用户会把“导入完成”理解成“风险已经处理好了”
- 如果 jobs、channels、MCP 立刻开始工作，就会出现“我只是导入，怎么它已经开始对外动作了”的反直觉行为

建议：
- 对 imported / untrusted 包统一定义“导入后静默态”
- 用户完成 review 之后，再逐项恢复能力

### 4) “本地快照 / 导出包 / 导入包”三个概念仍容易混淆

问题：
- 技术上都放在 `snapshot` 下
- 但用户视角下，这是三种完全不同的事情：
  - 本地快照：本机回滚
  - 导出包：跨设备迁移或分享
  - 导入包：外部内容进入本机

影响：
- V1 很容易把“备份”和“迁移包”混成一个概念
- 用户会误以为本地快照也适合分享，或者导出包也总能安全恢复

建议：
- 在文档中补一张操作对照表，至少写清：
  - 目的
  - 使用命令/API
  - 是否默认包含 secrets
  - 是否适合分享
  - 风险提示

### 5) 全局快照适合做“创建”，不适合在 V1 做重恢复

问题：
- 文档支持 `--include-global` 和 `snapshot create --all`
- 同时暗示未来可以恢复整个系统

影响：
- 全局恢复比单 workspace 恢复复杂很多：
  - 多 agent 配置冲突
  - `config.json` 合并/覆盖策略
  - `skill_pool` 冲突
  - 目标机器已有 agent 时的行为
- 这些都不是 V1 容易一次做对的

建议：
- V1 可以保留“创建全局快照”
- 但“全局恢复”建议降级：
  - 只支持导入检查
  - 或只支持恢复到隔离环境/新目录

### 6) 历史会话的不兼容提示暴露得太晚

问题：
- 当前设计是：导入时不阻塞，首次打开聊天时如果反序列化失败，再提示“已归档”

影响：
- 用户在导入完成时会以为所有会话都可用
- 等到点开才发现坏掉，会把问题理解为运行时 bug，而不是迁移兼容性问题

建议：
- 导入阶段就预扫描 `sessions/*.json` 的可读性或兼容性
- 导入完成时直接给出汇总：
  - 可用会话数
  - 已归档会话数
  - 不兼容原因

### 7) 缺少“导入后待办清单”这一层闭环

问题：
- 现在有“可运行性检查”，但还缺最后一步：用户下一步该去哪修

影响：
- 用户看到 warning，并不代表知道怎么把 agent 恢复到可用状态
- 如果没有统一入口，V1 会显得很碎，用户需要在多个页面和命令之间来回跳

建议：
- UI 提供“导入后待办清单”或“修复向导”
- CLI 至少输出一个有顺序的 checklist，包含：
  - 配置 provider
  - 重新认证 channels
  - 安装 skill dependencies
  - 审核并启用 skills
  - 恢复/启用 jobs

## 建议的 V1 范围

如果目标是“第一版先做稳”，建议优先保证以下四条闭环：
- 本地快照创建 + 原地恢复
- 导出包 + 导入到新 agent
- 导入后的统一隔离状态
- 导入完成后的 checklist / 修复入口

建议延期或降级的内容：
- 复杂的全局恢复
- 过度智能的跨平台路径自动修复
- 多种加密格式并存

## 建议补充的测试用例

### P0
- 篡改导入包中的 `manifest.trust_level=local`，验证导入后 skills 仍然 disabled
- 在以下恢复边界分别崩溃并重启，验证都能回到确定状态：
  - old -> backup 完成后
  - staging -> workspace 完成后、写 `applied` 前
  - 写 `applied` 后、verify 前
- restore 进行中同时触发后台 `reload_agent()`，验证不会出现双实例指向同一 `workspace_dir`

### P1
- 不受信任导入包包含 `jobs.json`、修改后的 `agent.json`、修改后的 `AGENTS.md`，验证不会在用户未审核前自动触发外部行为
- 密码错误、密文损坏、header 版本不支持三种情况，验证错误文案与错误码是否符合预期
- 重复点击 import/export/create/restore，验证互斥提示一致且任务状态可恢复

## 对 `response.md` 的评价

合理且值得保留的部分：
- skills 默认禁用
- 恢复状态机思路
- 资源限制
- 两阶段反馈
- Agent ID 冲突默认重命名

仍未闭环的部分：
- `trust_level` 被当作可信策略输入
- restore 崩溃状态机未推演完整
- 并发模型未对接 manager-level reload
- 第三方导入风险被过度收敛到 `skills`

## 最终判断

这版设计已经比上一版成熟很多，但还没有达到“可以放心开工实现”的程度。

当前最应该先定死的三件事：
- 不允许 manifest 内的 `trust_level` 参与任何安全决策
- 重做 restore 状态落盘方式和崩溃恢复推演
- 用统一的 per-agent lifecycle lock 串起 reload / snapshot / restore / import

如果这三件事不先收敛，后续实现很容易出现一种情况：
**文档看起来合理，但真正跑起来时，各个流程会互相打架。**
