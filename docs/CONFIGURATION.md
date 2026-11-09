# 配置参数说明 (CONFIGURATION.md)

本插件 v0.1.1 的配置说明。原则：**只记录代码中真实生效的配置项**；无法兑现的旧配置项已从
`_conf_schema.json` 移除，并在文末单独说明。

配置来源有两条：

1. **AstrBot 插件配置面板**（`_conf_schema.json` 暴露的项，见第 1–4 节）；
2. **每群策略（`group_policies` 表）**：由 WebUI「群组策略」页写入数据库；配置文件中的 `groups:`
   仅作为**首次种子**（数据库无该群记录时读取并落库）。见第 5 节。

---

## 1. 全局配置

| 参数键 | 类型 | 默认 | 实际行为 |
|---|---|---|---|
| `enabled` | bool | `true` | 总开关。`false` 时闸门对所有消息 `ALLOW`（不拦截、不处理准入指令）。 |
| `default_policy` | string | `"deny"` | **未显式配置群**的默认策略。`deny`：该群视为未启用准入（`GROUP_DISABLED`，拒绝）；`allow`：生成“默认启用”的群策略——**注意仍要求 ACTIVE 授权或按群豁免**，`allow` 不等于“无需授权”。 |
| `owner_qq_ids` | list | `[]` | 插件 Owner 的 QQ 号（字符串）。拥有群策略、审核员增删、黑名单、手动授权等特权；按群策略 `owner_bypass` 可豁免准入。 |
| `reviewer_qq_ids` | list | `[]` | 审核员 QQ 号。可审批/拒绝/撤销；**不含** Owner 专属操作。注意：审核员增删会以 Migration V5 的 `reviewer_overrides` 持久化，移除后重启不会从旧配置“复活”。 |

---

## 2. 申请行为（`application`）

| 参数键 | 类型 | 默认 | 实际行为 |
|---|---|---|---|
| `expire_hours` | int | `72` | 待审申请有效期；过期申请在启动清理或管理员操作时标记 `EXPIRED`，不能被批准。 |
| `reject_cooldown_hours` | int | `24` | 被拒绝后再次申请的冷却时间。 |
| `rate_limit_per_hour` | int | `5` | 单用户每小时最大申请次数；检查+自增在 `BEGIN IMMEDIATE` 事务内原子执行，并与申请创建同事务。 |

> 已移除 `application.duplicate_pending_allowed`：数据库 `uq_apps_pending` 部分唯一索引始终禁止同一
> `(bot_self_id, group_id, user_id)` 存在多条 `PENDING` 申请，该开关无法兑现，已不再暴露。

---

## 3. 私聊与通知

### 3.1 `private_chat`

| 参数键 | 类型 | 默认 | 实际行为 |
|---|---|---|---|
| `enabled_for_normal_users` | bool | `false` | 普通用户私聊普通对话开关。`false` 时非 Owner/Reviewer 的私聊普通消息被拒绝（准入指令仍可用）。 |
| `admin_commands_enabled` | bool | `true` | 预留项：当前版本未在鉴权路径读取（私聊 `/ga` 的角色校验由服务端 `AuthorizationService`/`CommandDispatcher` 完成）。**不要依赖它**做安全边界。 |

### 3.2 `notifications`

| 参数键 | 类型 | 默认 | 实际行为 |
|---|---|---|---|
| `reviewer_private_message` | bool | `true` | 新申请是否私聊提醒 Owner/Reviewer。`false` 时不再发管理员私聊提醒（若配置了 `fallback_group_id` 且无收件人，可由专用群通知路径处理）。 |
| `fallback_group_id` | string | `""` | **管理员**通知私聊失败时的专用审核群号（绝不会使用申请来源群）。 |
| `retry_enabled` | bool | `true` | 是否启动 outbox 后台重试 worker。 |
| `max_retries` | int | `5` | 单条通知最大发送尝试次数。 |
| `retry_interval_seconds` | int | `60` | 指数退避基础间隔（实际间隔 = base × 2^(attempt-1)）。 |
| `user_group_fallback_enabled` | bool | `false` | **申请人**结果通知的群内兜底开关。默认关闭；开启后私聊失败达到上限才向申请来源群发送最小文案。 |
| `user_group_fallback_text` | string | `""` | 上述群内兜底的文案；**不得包含审批理由/备注**；为空时使用内置最小文案（不含敏感信息）。 |

**隐私要点**：申请人审批结果的完整内容（含备注/拒绝原因）**默认仅私聊**发送；群内兜底发送的是
`fallback_payload` 短文案，不是完整通知。管理员通知仅回退到 `fallback_group_id` 专用审核群。

---

## 4. 安全与审计（`security`）

| 参数键 | 类型 | 默认 | 实际行为 |
|---|---|---|---|
| `log_denied_messages` | bool | `false` | 是否把被闸门拦截的消息文本（截断 100 字符）写入 INFO 日志。 |
| `keep_audit_days` | int | `90` | 审计日志保留天数。**在插件启动时**（`initialize()`）清理更早记录；`0` 表示不清理。**不是**后台周期任务。 |

> 已移除 `security.fail_closed` 与 `security.gate_priority`：fail-closed 是固定行为（不可关闭）；
> 闸门优先级固定为 `GATE_PRIORITY = 50`（改动它需要改代码并同步 GroupGuardian 补丁与框架测试），
> 配置项无法生效，故不再暴露。

---

## 5. 每群独立策略（`group_policies`，WebUI「群组策略」）

每群策略存储在 SQLite `group_policies` 表（WebUI 可改）；配置文件中的 `groups` 块仅在数据库无该群记录时
作为种子读取并落库。

| 字段 | 默认 | 实际行为 |
|---|---|---|
| `enabled` | `true` | 该群是否启用准入；`false` 时群内所有消息按 `GROUP_DISABLED` 拒绝。 |
| `min_level` | `10` | 申请/复核的最低群成员等级。 |
| `approval_required` | `true` | **当前仅存储与展示**：放行始终要求 ACTIVE 授权或按策略豁免，不会因 `false` 自动授予访问。请勿把它理解为“免审批自动开通”。 |
| `grant_duration_days` | `0` | 授权有效期（天）；`0` = 永久。 |
| `recheck_interval_seconds` | `300` | 等级复核间隔（秒）。 |
| `restore_policy` | `"automatic"` | 等级掉出后：`automatic`=等级回升自动恢复；`manual`=保持挂起需人工处理。 |
| `owner_bypass` | `true` | 插件 Owner 是否豁免该群准入。 |
| `group_admin_bypass` | `false` | QQ 群主/群管理员是否豁免该群准入（默认关闭，避免群管自动获得 Bot 特权）。 |

WebUI 的等价配置入口：群组策略页 → `POST /api/v1/plugins/extensions/astrbot_plugin_group_approval/groups/update`
（需要已认证的 Dashboard 管理员；变更需带纯数字 `bot_self_id`）。

---

## 6. 已移除/不可用配置项（避免误解）

| 旧配置 | 原因 |
|---|---|
| `application.duplicate_pending_allowed` | 部分唯一索引始终禁止重复 PENDING，开关无效，已移除。 |
| `security.fail_closed` | fail-closed 固定生效，不能关闭。 |
| `security.gate_priority` | 闸门固定 50；改它需改代码并同步补丁/框架测试，配置无效。 |
| `private_chat.admin_commands_enabled` | 当前未在鉴权路径读取（保留字段），不应作为安全开关。 |
| `application.duplicate_pending_allowed` / `groups` 块在插件面板中 | `groups` 块不在 `_conf_schema.json` 中，请用 WebUI 群组策略或数据库管理；手改配置文件仅作种子。 |
