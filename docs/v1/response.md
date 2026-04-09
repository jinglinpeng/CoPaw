# Snapshot 设计 Review 回复

针对 `docs/review.md` 的逐条回复。

---

## 主要问题回复

### 1) 完整性校验可被伪造（Critical） -- 部分采纳

**Review 意见：** manifest 中的 checksums 无签名机制，攻击者可同时篡改文件和 hash。

**回复：**

Reviewer 混淆了两个不同的安全目标：

- **完整性（Integrity）**：检测传输/存储过程中的意外损坏。Checksums 解决的是这个问题。
- **真实性（Authenticity）**：验证内容来自可信来源。这需要签名或 HMAC。

对于 CoPaw 的场景，快照的来源信任链如下：

- **本地快照**：用户自己创建、自己恢复，不存在第三方篡改问题。Checksums 用于防损坏，足够。
- **导入第三方快照**：这里确实没有信任基础。但签名/HMAC 的前提是有 PKI 或预共享密钥，CoPaw 作为本地部署的开源项目，不具备这个基础设施。即使加了签名，攻击者也可以用自己的密钥签名一个恶意包。

**真正的防线不在校验和，而在行为层面**（见第 2 点）。签名机制适合有中心化分发平台的场景（如 App Store），不适合 CoPaw V1 的点对点分享模型。

**采纳部分：**

- 在文档中明确 checksums 的定位是"防损坏，不防篡改"
- 在 manifest 中增加 `trust_level` 字段区分 `"local"` 和 `"imported"`

**不采纳部分：**

- V1 不引入签名/HMAC 机制。如果未来建设了快照分发平台（如社区市场），再引入签名体系。

---

### 2) 导入恶意代码的防护不足（Critical） -- 完全采纳

**Review 意见：** 仅"标记为未审核"不够，应默认禁用执行。

**回复：** 完全同意。"标记为未审核"但仍可执行是无效的安全屏障。

**修改方案：**

- 导入的 skills 在 `skill.json` 中设置 `enabled: false`
- Console UI 中对导入的 skills 显示"待审核"标签，需要用户逐个审核并手动启用
- CLI 导入时输出明确提示：`"已导入 N 个技能，均已禁用。请使用 copaw skill enable <name> 逐个启用。"`
- 导入的 skills 首次启用时，显示技能内容摘要供用户确认

---

### 3) "默认不含敏感数据"与"本地快照默认含 secrets"策略冲突（High） -- 部分采纳

**Review 意见：** 双重默认语义冲突，用户易误判"本地=安全"。

**回复：**

这不是"冲突"，而是针对不同场景的分层策略，但文档表述确实容易引起混淆。实质区别是：

- **本地快照**（存在 `{WORKING_DIR}/snapshots/`）：与 `SECRET_DIR` 在同一台机器上，secrets 本来就在磁盘上。不包含 secrets 的本地快照恢复后需要用户重新配置所有 API Key，体验极差。
- **导出文件**（用户主动下载的 ZIP）：会离开本机，所以默认剥离 secrets。

**采纳部分：**

- 重写文档，用"本地快照"和"导出包"明确区分两个概念，消除语义歧义
- 增加"本地快照不建议上传到网盘/IM"的文档警告
- Windows 平台的权限处理：不使用 POSIX `0600`，改为使用 Python `os` 模块或平台特定 API 设置 ACL

**不采纳部分：**

- 不改变"本地快照包含 secrets"的默认行为。如果本地快照不含 secrets，每次恢复都要重新输入所有 API Key，这会导致功能基本不可用，产生远多于安全风险的用户投诉。

---

### 4) 一致性锁范围定义不完整（High） -- 采纳

**Review 意见：** 锁的覆盖范围不清晰，Channel 消息重试依赖可靠队列但未定义。

**回复：** 同意，设计文档对锁的精确语义定义不足。

**修改方案 - 锁覆盖的入口点：**


| 入口点                         | 锁行为                                               |
| --------------------------- | ------------------------------------------------- |
| `AgentRunner.run()` (聊天请求)  | 持有读锁                                              |
| `CronManager` 任务执行          | 持有读锁                                              |
| `Workspace.reload()`        | 持有写锁（与 snapshot 互斥）                               |
| `SnapshotManager.create()`  | 持有写锁                                              |
| `SnapshotManager.restore()` | 直接调用 `workspace.stop()`，不需要读写锁（workspace 停止后没有竞争） |
| Channel 消息接收                | 不加锁，消息进入 runner 时才触碰读锁                            |
| MCP 回调                      | 属于 runner 请求的一部分，已在读锁范围内                          |


**关于 Channel 消息"可重试"的澄清：**

- CoPaw 的 Channel 是推送模型（webhook/长轮询/websocket），不是可靠队列
- Snapshot 期间（持有写锁），新的聊天请求在 `runner.run()` 入口处等待锁（而非丢弃）
- 等待超时后返回"系统正在维护"的错误响应，由 Channel 层决定是否重试
- 这是 **设计上可接受的降级**，不需要引入消息队列

---

### 5) 恢复"原子替换"在跨平台目录级不成立（High） -- 采纳

**Review 意见：** Windows 下目录替换受文件占用影响，不是原子操作。需要恢复事务状态机。

**回复：** 完全正确。"原子替换"是对 Linux `rename()` 语义的过度泛化，在 Windows 上不成立。

**修改方案 - 恢复流程改为三阶段状态机：**

```
PHASE 1: PREPARE
  - 校验 manifest 和 checksums
  - 检查磁盘空间（需要 2x snapshot 大小 + 当前 workspace 大小）
  - 解压 snapshot 到 `{WORKING_DIR}/_restore_staging/{agent_id}/`
  - 写入状态文件 `{WORKING_DIR}/_restore_staging/{agent_id}/.restore_state` = "prepared"

PHASE 2: APPLY
  - 调用 workspace.stop(final=True) 关闭所有服务、释放文件句柄
  - 将当前 workspace 重命名为 `{workspace_dir}.backup.{timestamp}`
  - 将 staging 目录重命名为 workspace_dir
  - 写入状态文件 .restore_state = "applied"

PHASE 3: VERIFY
  - 启动 workspace (workspace.start())
  - 验证核心服务可用
  - 成功：删除 .restore_state，保留 backup 目录（用户可手动清理或由 prune 清理）
  - 失败：回滚到 backup 目录，重启
```

**崩溃恢复（启动时检查）：**

- 发现 `.restore_state = "prepared"`：staging 目录存在但未 apply，清理 staging，正常启动原 workspace
- 发现 `.restore_state = "applied"`：apply 完成但未 verify，尝试启动新 workspace；失败则回滚到 backup
- 发现 `{workspace_dir}.backup.`* 但无 `.restore_state`：正常状态，backup 是历史遗留，可被 prune 清理

---

### 6) 加密方案细节缺失（Medium） -- 部分采纳

**Review 意见：** 仅写"AES-256"，未定义 KDF、盐、nonce、AEAD 模式等。

**回复：** 设计文档的定位是方案设计而非实现规格，不需要 RFC 级别的密码学参数定义。但指定关键选型是合理的，可以避免实现阶段的低级错误。

**修改方案 - 补充加密选型：**

- 使用 Python `cryptography` 库的 **Fernet**（基于 AES-128-CBC + HMAC-SHA256，内置 IV 管理和认证）
- 或 AES-256-GCM（AEAD，自带认证标签）
- KDF：使用 **PBKDF2-HMAC-SHA256**（`cryptography` 库内置），迭代次数 >= 600,000
- 每个加密包包含版本头（1 byte）+ 随机盐（16 bytes）+ 密文
- 密码错误时通过 HMAC/GCM tag 验证失败来检测，给出明确的"密码错误"提示（而非"文件损坏"）

**不采纳部分：**

- 不在设计文档中写完整的密码学协议规范，这属于实现文档的范畴

---

### 7) 跨版本兼容策略过于宽泛（Medium） -- 部分采纳

**Review 意见：** 版本号不足以判断 `sessions/*.json` 的兼容性，建议 schema migration。

**回复：** 观察正确，但完整的 schema migration 体系对 V1 来说是过度工程。

`sessions/*.json` 包含 AgentScope 的 `state_dict()`，其 schema 由 AgentScope 库控制而非 CoPaw。CoPaw 无法为第三方库的内部状态做 migration。

**修改方案：**

- manifest 中增加 `agentscope_version` 字段
- 导入时如果 AgentScope 版本不匹配，session 文件标记为"可能不兼容"
- 不兼容的 session 文件：不阻塞导入，但在恢复后首次打开对应聊天时捕获反序列化错误，提示"此对话历史与当前版本不兼容，已归档"
- 配置文件（`agent.json`、`jobs.json` 等）由 CoPaw 控制 schema，这些文件的 migration 通过 `schema_version` + 迁移函数处理（已有 `json_repair` 等容错机制可复用）

---

### 8) 资源消耗与 DoS 防护不足（Medium） -- 采纳

**Review 意见：** 未定义 zip bomb、超大文件等资源限制。

**回复：** 同意，导入 API 确实需要资源限制。

**修改方案 - 导入资源限制：**


| 维度     | 限制                    | 可配置         |
| ------ | --------------------- | ----------- |
| 解压后总大小 | 默认 2GB                | 是，通过 config |
| 单文件大小  | 默认 500MB              | 是           |
| 文件数量   | 默认 10,000             | 是           |
| 最大路径深度 | 20 级                  | 否           |
| 最大路径长度 | 260 字符（兼容 Windows）    | 否           |
| 压缩比阈值  | 解压大小 / 压缩大小 > 100 时拒绝 | 否           |


- 解压过程中实时统计，超出任一限制立即中止并清理临时文件
- 符号链接和硬链接：解压时一律跳过，不创建

---

## 攻击性用户场景回复

### 场景 A：社群分享快照投毒

已通过第 2 点修改覆盖：导入的 skills 默认 `enabled: false`，需逐个审核启用。这是最务实的防线 -- 不依赖密码学基础设施，直接在行为层面阻断。

### 场景 B：同事误传"本地快照"

已通过第 3 点修改覆盖：文档明确区分"本地快照"和"导出包"概念，增加云同步风险警告。另外，本地快照存储路径（`{WORKING_DIR}/snapshots/`）通常不在常见的云同步目录中（如 OneDrive/iCloud），实际风险有限。

### 场景 C：恢复过程断电/进程崩溃

已通过第 5 点修改覆盖：引入三阶段状态机 + 启动时崩溃恢复检查。

### 场景 D：高并发消息 + snapshot

已通过第 4 点修改覆盖：明确了锁覆盖范围。Snapshot 持有写锁期间，新请求在 runner 入口处排队等待（而非丢弃），超时后返回维护提示。这是可接受的短暂降级，通常 snapshot 创建耗时在秒级。

### 场景 E：跨平台迁移

原设计已覆盖（导入时路径转换 + manifest 记录原始平台）。补充：在导入完成后的"可运行性检查"阶段（见下方 UX 建议回复），检测并报告路径相关问题。

### 场景 F：恶意导入包资源炸弹

已通过第 8 点修改覆盖：增加解压大小、文件数、压缩比等限制。

---

## 建议测试用例回复

所有建议的测试用例都是合理的，全部采纳纳入测试计划。按优先级排序：

**P0（阻塞发布）：**

- Zip Slip + 符号链接攻击
- 恢复中途崩溃后的 recover 流程
- 导入的 skills 默认禁用验证
- 密码错误 vs 文件损坏的区分

**P1（发布前完成）：**

- Zip bomb / 资源限制验证
- 并发 snapshot + restore 锁语义
- 跨版本 session 不兼容的优雅降级
- `--include-secrets` vs 默认导出的 diff 验证

**P2（发布后补充）：**

- 磁盘空间边界测试
- Windows 文件占用冲突下的恢复重试
- 多版本矩阵兼容性测试
- 高并发消息场景下的 snapshot 锁性能

---

## 面向用户体验的补强建议回复

### "恢复不回滚外部副作用"确认提示 -- 已有，补强措辞

原设计已包含此点。采纳 reviewer 建议，在 CLI 和 UI 中使用更具体的措辞：

> "恢复快照仅回滚 CoPaw 本地配置和数据。已通过渠道发送的消息、已执行的定时任务、已调用的第三方 API 不会被撤销。确认继续？"

### "导入成功"拆分为两阶段反馈 -- 采纳

修改导入流程输出：

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

### Agent ID 冲突默认选项改为"重命名" -- 采纳

修改冲突处理的默认行为：

- CLI：默认选项改为重命名（如 `default` -> `default-imported-20260408`），覆盖需要 `--force` 标志
- Console UI：对话框默认选中"重命名"，"覆盖"需要二次确认（输入 agent 名称确认）

### 高耗时任务的进度/取消/防重复 -- 部分已有，补充防重复

原设计已包含进度条。补充：

- API 层面：snapshot/restore 请求返回 `task_id`，客户端通过 WebSocket 或轮询获取进度
- 防重复：同一 agent 同时只允许一个 snapshot/restore 操作（互斥锁），重复请求返回 409 Conflict 和当前任务进度

### 含 secrets 导出的二次确认 -- 已有，补强

原设计已包含警告。补充：CLI 使用 `--include-secrets` 时要求用户输入 `YES` 确认（而非 `y/n`）。

### 导入第三方快照后默认禁用 skills -- 已在第 2 点采纳

---

## 必须补齐的设计空白回复


| 空白项               | 回复                                         |
| ----------------- | ------------------------------------------ |
| 快照真实性与来源可信度       | V1 不引入签名机制（见第 1 点回复）。防线在行为层：导入 skills 默认禁用 |
| 导入的 skills 是否默认禁用 | 是。`enabled: false` + 待审核标签（见第 2 点回复）       |
| 锁的精确定义            | 已补充入口点覆盖表（见第 4 点回复）                        |
| 恢复事务状态机           | 已补充三阶段状态机 + 崩溃恢复流程（见第 5 点回复）               |
| 导入资源限制阈值          | 已补充限制表（见第 8 点回复）                           |


---

## 总结


| Review 编号       | 严重级别     | 判定   | 动作                                     |
| --------------- | -------- | ---- | -------------------------------------- |
| 1) 校验可伪造        | Critical | 部分采纳 | 明确 checksums 定位；V1 不加签名                |
| 2) 恶意代码防护       | Critical | 完全采纳 | 导入 skills 默认禁用                         |
| 3) Secrets 策略冲突 | High     | 部分采纳 | 重写文档消除歧义；不改默认行为                        |
| 4) 锁范围不完整       | High     | 采纳   | 补充锁覆盖入口表                               |
| 5) 恢复非原子        | High     | 采纳   | 改为三阶段状态机                               |
| 6) 加密细节缺失       | Medium   | 部分采纳 | 补充关键选型；不写完整协议规范                        |
| 7) 跨版本兼容        | Medium   | 部分采纳 | 增加 agentscope_version；session 不兼容时优雅降级 |
| 8) DoS 防护       | Medium   | 采纳   | 补充资源限制表                                |


