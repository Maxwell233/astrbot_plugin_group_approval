# 安全与边界防御设计 (SECURITY.md)

本文件描述 `astrbot_plugin_group_approval` **v0.1.1** 的安全边界。所有“已实现”条目均在当前代码中有对应实现
与测试；**真实 QQ/NapCat 环境未实测**（见文末状态表）。缺陷与修复的完整清单见
[docs/DEFECT_MATRIX.md](DEFECT_MATRIX.md)。

---

## 1. 身份与鉴权原则

- **QQ 侧身份**：以 `(bot_self_id, group_id, user_id)` 作为业务主键；`bot_self_id` 必须是**已核验的
  Bot QQ 号**，`user_id` 来自事件 `sender.user_id`，`group_id` 来自事件/消息对象。绝不根据昵称、
  群名片、转发内容或 LLM 推导判断权限。
- **QQ 侧角色**：
  - **Owner**（`owner_qq_ids`）：群策略、审核员增删、黑名单、手动授权。
  - **Reviewer**（`reviewer_qq_ids`）：审批/拒绝/撤销。
  - **Approved User**：普通获批用户，**不会**继承 AstrBot/Dashboard 管理权限。
  - **QQ 群主/群管理员 ≠ Bot 管理员**：仅当对应群策略 `group_admin_bypass=true` 时按群豁免准入。
- **WebUI 侧身份与 QQ 侧彻底分离**：AstrBot 只有**一个 Dashboard 账号**（`dashboard.username`）；
  QQ 的 Owner/Reviewer QQ 号列表**不会**赋予任何 Dashboard 权限（见第 4 节）。

---

## 2. 故障阻断（Fail-Closed）

以下情况**一律拒绝**（不会因异常而放行）：

- NapCat 请求超时、连接断开、返回 `status=failed`/异常 retcode；
- 目标群成员已退群/被移出（`is_member=false`、错误码）；
- 等级字段缺失/为空/无法解析；
- **无法核验当前 Bot 身份**（事件没有可信 `self_id`，且 `get_login_info` 未能验证）；
- 授权状态/版本在并发中发生变化（`revision` 条件更新未命中时按当前真实状态重新判定，宁可拒绝）。

实现：`approval/napcat_client.py`、`approval/level_service.py`、`approval/access_service.py`。
注意：`security.fail_closed` 配置项已从 `_conf_schema.json` 移除（无法通过关闭它变成 fail-open；
fail-closed 是固定行为）。

---

## 3. GroupGuardian 主审核顺序

AstrBot 4.29.4 通过最内层装饰器建立 Handler 优先级。使用修订版 [补丁](../patches/README.md)，让 GroupGuardian 主审核注册为 100，先于 Group Approval 的 50。两个入口都必须在目标会话启用。

`VERIFIED` 表示启用的群消息入口具备正确的平台过滤、优先级和实际注册顺序。它无法证明审核规则生效、消息类型覆盖或 QQ 撤回成功。若需要审核先行，收到 `CONFLICT`、`DISABLED`、`NOT_INSTALLED` 或 `UNVERIFIED` 应暂停受管 Bot 群消息接入，修复配置并复核。插件继续执行准入，避免配置检查失败时所有业务直接放行。

诊断列出闸门前的其他 Handler 供部署时核对。更早的过滤器、`SessionWaiter`、独立任务和其他来源调用需各自执行权限控制。 [兼容性说明](../COMPATIBILITY.md)

---

## 4. WebUI 身份认证与服务端授权

- **唯一官方身份信号**：`request.username`。它由 AstrBot 在完成 JWT/API key 鉴权后注入；客户端请求头
  （`X-AstrBot-User`、`X-User-Id`、`Authorization-User` 等）**一律不信任**。
- **唯一 Dashboard 管理员**：只有 `request.username` **完全等于** AstrBot 配置的 `dashboard.username`
  才被授予插件 OWNER。AstrBot **没有** Dashboard 角色表，也不存在“多用户角色映射”。
- **QQ 身份不参与 Dashboard 鉴权**：QQ 的 Owner/Reviewer 号即使与 Dashboard 用户名相同也无特权；
  这两套身份系统不交叉（`approval/auth_service.py:50-161`）。
- **API Key**：以 `api_key:` 开头的机器身份可读，但**不能**执行变更操作。
- **未知/无法证明身份**：拒绝所有管理操作。
- **变更操作**：七个写接口均检查 `bot_self_id` 的数字格式和当前 OneBot 连接上的定向登录验证。缺失或格式非法返回 400，无法验证当前连接返回 403。批准与拒绝另外核对申请所属 Bot。
- 概览、列表与审计查询支持按 Bot 筛选。Dashboard 管理员可以查看全部 Bot。客户端写入操作绑定记录所属 Bot，并由服务端再次验证。
- **HTTP 语义说明**：AstrBot 网关会在 Handler 之前拒绝未认证请求；插件对“已认证但无权限”的响应是
  **HTTP 200 + body `retcode`（401/403）**，请以 `retcode` 判断，不要仅看 HTTP 状态码。
- 响应中不回显服务器内部异常堆栈；参数校验错误返回结构化错误信息。

---

## 5. Bot 身份与多账号路由

反向 WebSocket 连接表仅提供候选账号。每个账号通过定向 `get_login_info` 验证，缓存按平台实例和 QQ 组合保存，实时检查断线、客户端替换和 socket 替换。所有业务调用按已核验 QQ 显式路由。旧客户端的短时事件候选只用于兼容业务调用，Dashboard 写入仍要求新探测。

OneBot 消息里的 `self_id` 由协议端提供。应对反向 WebSocket 端口启用认证并仅允许可信 NapCat 接入。SDK 的连接表为内部接口，升级后应复跑多账号测试。

---

## 6. 通知隐私（Notification privacy）

- **申请人结果通知默认仅私聊**：包含审批备注/拒绝原因的完整内容只发往申请人私聊。
- **群内兜底默认关闭**：仅当 `notifications.user_group_fallback_enabled=true` 时，私聊失败达到重试上限
  才向申请人来源群发送 **`fallback_payload` 短文案**（不含审批理由/备注）；文案由
  `user_group_fallback_text` 配置，缺省为不含敏感内容的最小提示。
- **管理员通知**：优先私聊 Owner/Reviewer；私聊失败仅允许回退到**显式配置的专用审核群**
  （`notifications.fallback_group_id`）。新通知拒绝把申请来源群作为专用审核群。历史 outbox 若配置过该群，部署前检查仍待发送的旧记录。
- 缺少 Bot 身份的历史 outbox 行标记 `FAILED`，由操作员处理。审核详情、批准、拒绝、手动授权和审核员管理移到当前 Bot 私聊。群内申请回复不包含审核备注或封禁原因。
- **事件去重**：`event_key` 使用稳定业务标识（申请编号 / 用户结果类型+申请编号），入队
  `ON CONFLICT DO NOTHING`；因此重试不会重复入队。
- **投递语义**：outbox 为 **at-least-once**（QQ/OneBot 无 exactly-once 原语）；worker 以事务原子
  claim（`SENDING` + `claimed_at`），超时可回收；最终失败行保留 `last_error`，需人工排查。
  **不宣称 exactly-once 投递。**

实现：`approval/notification_service.py`、`approval/migrations.py`（V2 唯一索引、V6 `fallback_payload`/`claimed_at`）。

---

## 7. 数据库事务与并发安全

- **协程隔离的事务**：每个 `transaction()` 使用**独立 SQLite 连接** + `BEGIN IMMEDIATE`，并在
  `contextvars` 中登记事务连接，使同一 Task 内的数据库调用自动路由到该连接；没有继承该上下文的其他协程使用共享
  autocommit 连接（`isolation_level=None`）；继承上下文的子任务不能使用父任务事务，各数据库实例的上下文独立（`approval/database.py:1-238`）。
  旧的“单一共享连接 + 进程级 `_in_transaction` 布尔值”实现已删除。
- **WAL + `busy_timeout` + 外键**：连接统一设置 `journal_mode=WAL`、`synchronous=NORMAL`、
  `foreign_keys=ON`、`busy_timeout=5000`。
- **审批原子性**：一次成功审批在**一个事务**内完成：重读校验申请状态/过期 → 条件 `UPDATE ...
  AND status='PENDING'` 并校验 rowcount → grant upsert → 审计日志 → 通知入队；任何失败整体回滚。
  网络等级查询在事务**之外**执行，避免慢网络持有写锁（`approval/approval_service.py:387-520`）。
- **授权状态乐观并发（`revision`，Migration V4）**：所有自动/手动状态转换使用
  `WHERE status=? AND revision=?` 条件更新并校验 rowcount；并发撤销/封禁优先于自动恢复，过期授权不可被
  自动恢复“复活”（`approval/access_service.py`、`approval/approval_service.py`）。
- **迁移（V1–V6）**：全部为增量、可重复执行；V2 在创建 outbox `event_key` 唯一索引前先对旧数据去重，
  升级旧库不会因重复键中止；V4 增加 `revision`；V5 增加 `reviewer_overrides`；V6 增加
  `fallback_payload`/`claimed_at`；V1–V3 数据保留（`approval/migrations.py`）。
- 事务体内**禁止**网络 I/O（例如通知发送由 outbox worker 在事务提交后执行）。

---

## 8. LivingMemory 与验证范围

原版 LivingMemory 的被动过滤器在 `WakingCheckStage` 安排任务。部署时关闭 `session_manager.enable_full_group_capture`，或应用 [兼容补丁](../patches/README.md) 将 AIOCQHTTP 被动采集延后到群消息准入通过。历史数据、主动工具、总结、召回及共享记忆策略分别核对。

本轮已运行真实 AstrBot 4.29.4 处理阶段和 aiocqhttp 1.4.4 路由组件测试。Windows CI 覆盖单元/服务测试。Windows Launcher、真实 NapCat、QQ 群及两个第三方插件完整部署待联调。 [测试报告](../INTEGRATION_TEST_REPORT.md)
