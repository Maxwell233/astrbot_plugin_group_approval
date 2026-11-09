# 配置参数完全说明 (CONFIGURATION.md)

本插件提供可视化 WebUI 配置面板与 SQLite 动态策略双轨配置。

---

## 1. 核心全局配置项

在 AstrBot WebUI 插件面板中可直观配置下列参数：

| 参数键 | 类型 | 默认值 | 详细说明 |
|---|---|---|---|
| `enabled` | `bool` | `true` | 插件主功能总开关。设为 `false` 时跳过所有准入拦截。 |
| `default_policy` | `string` | `"deny"` | 未配置群的默认准入策略。`deny`（必须配置后才开放申请）或 `allow`（未配置群不设准入门槛）。 |
| `owner_qq_ids` | `list` | `[]` | 插件最高管理员 QQ 号列表。享有全局审批、配置策略、手动授权、拉黑与管理员增删权限。 |
| `reviewer_qq_ids` | `list` | `[]` | 审核员 QQ 号列表。享有待审查看、审批、拒绝与撤销授权权限。 |

---

## 2. 申请行为配置 (`application`)

| 参数键 | 类型 | 默认值 | 详细说明 |
|---|---|---|---|
| `expire_hours` | `int` | `72` | 申请有效期（小时）。超时未审核的申请将自动转为 `EXPIRED`。 |
| `reject_cooldown_hours` | `int` | `24` | 申请被拒绝后的再次申请冷却时间（小时）。防止被拒用户频繁骚扰。 |
| `duplicate_pending_allowed` | `bool` | `false` | 是否允许同一用户在同一群存在多条待审申请。生产环境强制保持 `false`。 |
| `rate_limit_per_hour` | `int` | `5` | 单用户每小时最大提交申请次数（滑动窗口防刷机制）。 |

---

## 3. 私聊策略 (`private_chat`)

| 参数键 | 类型 | 默认值 | 详细说明 |
|---|---|---|---|
| `enabled_for_normal_users` | `bool` | `false` | 是否允许普通用户通过私聊使用 Bot 普通业务功能。为防止绕过群等级，默认禁用。 |
| `admin_commands_enabled` | `bool` | `true` | 是否允许 Owner / 审核员在私聊中使用 `/ga` 管理指令。 |

---

## 4. 通知机制 (`notifications`)

| 参数键 | 类型 | 默认值 | 详细说明 |
|---|---|---|---|
| `reviewer_private_message` | `bool` | `true` | 新申请提交后是否向管理员发送 QQ 私聊审批通知。 |
| `fallback_group_id` | `string` | `""` | 私聊发送失败或受风控限制时的备用审核通知群号。 |
| `retry_enabled` | `bool` | `true` | 启用持久化发信队列表（`notification_outbox`）与指数退避重试。 |

---

## 5. 安全策略 (`security`)

| 参数键 | 类型 | 默认值 | 详细说明 |
|---|---|---|---|
| `fail_closed` | `bool` | `true` | 故障阻断模式。当 NapCat 或网络查询超时时，严格判定不满足准入条件。 |
| `log_denied_messages` | `bool` | `false` | 是否在日志中记录被拦截消息内容。 |
| `keep_audit_days` | `int` | `90` | 审计日志保留天数。系统会定期自动归档清理过期日志。 |

---

## 6. 每群独立策略说明 (GroupPolicy)

每个群可配置专属准入规则：
```yaml
groups:
  "123456789":
    enabled: true                     # 该群开启准入
    min_level: 15                     # 申请最低群成员等级为 LV15
    approval_required: true           # 达到等级后必须提交申请并由管理员批准
    grant_duration_days: 30           # 授权有效期 30 天（0 表示永久）
    recheck_interval_seconds: 300     # 每 300 秒复核一次群成员等级
    restore_policy: "automatic"       # 等级恢复后自动解除冻结（或 "manual" 手动重新审批）
    owner_bypass: true                # 插件 Owner 是否豁免准入
    group_admin_bypass: false         # QQ群管理员是否自动豁免门槛
```
