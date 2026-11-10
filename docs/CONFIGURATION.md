# 0.1.2 配置

## AstrBot 插件配置

- `enabled` 默认开启，启用时安装 AstrBot 4.29.4 原生聊天兼容层。
- `owner_qq_ids` 配置 QQ Owner。开启 `inherit_astrbot_admins` 后，AstrBot 全局 `admins_id` 中的数字 ID 也按 QQ Owner 识别。开关默认关闭，只在这些数字 ID 都是可信 QQ 号时开启。会话级管理员和 QQ 群管理员不用于此判定。
- `reviewer_qq_ids` 是初始审核员，之后可由 Owner 私聊管理。要接收私聊审核通知，请将 QQ 明确加入插件的 Owner 或审核员列表。
- `private_chat.admin_commands_enabled` 控制所有 QQ `/ga` 管理命令。Dashboard 使用独立的 AstrBot 管理员认证。
- `application.query_limit` 默认 3，`application.query_window_seconds` 默认 60。每个 Bot、用户、域在窗口内可发起的申请等级核验次数独立计数，NapCat 查询失败或等级不够也计数。已有授权或仍需审批的待审申请通常直接返回状态。
- `application.rate_limit_per_hour` 默认 5，仅限制每个 Bot、用户、域每小时新建的待审申请数。
- `notifications.retry_enabled` 默认启动私聊通知发送器，发送失败按配置重试。
- `security.keep_audit_days` 默认 90，仅在启动时执行审计清理。设为 0 保留全部。

## 每个 QQ Bot 的设置

在 Dashboard 选中在线 Bot 后保存以下设置。主群需通过 Bot 自身的成员查询验证。
每个域优先使用已保存的门槛，尚未保存时使用表中的默认值。

| 项目 | 默认值 |
|---|---|
| 主群 | 未配置 |
| GROUP 最低等级 / 审批 | 20 / 开启 |
| PRIVATE 最低等级 / 审批 | 50 / 开启 |
| GROUP → PRIVATE 免审 | 关闭 |
| PRIVATE → GROUP 免审 | 关闭 |
| Owner 原生聊天豁免 | 关闭 |
| 主群等级缓存 | 300 秒，0 表示每次使用时查询 |

跨域免审需要主动申请，等级和身份核验仍按目标域执行。启用 Owner 聊天豁免会允许已配置的 QQ Owner 跳过普通域授权，撤销和黑名单限制仍会检查。Owner 可单独管理两种授予豁免。撤销后需要 Owner 重新授予，暂停可以主动申请恢复。解除黑名单会要求重新核验资格，已有人工撤销限制仍然生效。更换主群会取消该 Bot 旧主群的待审申请，用户需按新主群重新提交。
