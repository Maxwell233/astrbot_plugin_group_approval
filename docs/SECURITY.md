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

## 3. GroupGuardian：审核优先级是**部署要求**，不是运行时补丁

**事实（AstrBot 4.29.4 / 上游 GroupGuardian v2.8.11）：**

- Handler 执行顺序由 `extras_configs["priority"]` **降序**决定；优先级 50 的准入闸门先于优先级 0 的
  GroupGuardian 主审核 Handler 执行（`astrbot/core/star/star_handler.py:19-26`）。
- 因此上游 GroupGuardian **默认会被本插件闸门抢先**：闸门 `stop_event()` 后，GroupGuardian 审核
  **不会运行**（`star_request.py:39-41,88-89` 的 break 行为）。

**本插件的做法：**

1. **不修改**任何第三方 Handler（不做运行时 monkey-patch；AstrBot 也不支持安全地这样做）。
2. 要求部署方应用兼容补丁 `patches/group_guardian_priority.patch`：仅把 GroupGuardian 主审核
   Handler 的装饰器改为 `priority=100`。补丁说明、许可证（上游 MIT）见 `patches/README.md`。
3. 启动时**只读**真实注册表并报告（`approval/diagnostics.py`，`main.py:309-323`）：

| `moderation_precedence` | 含义 | 安全含义 |
|---|---|---|
| `VERIFIED` | GroupGuardian Handler 实际优先级 > 闸门（50） | 当前进程注册表中审核先于准入 |
| `CONFLICT` | 发现 GroupGuardian 但优先级 <= 50 | **不保证**未授权用户消息被审核，需立即打补丁 |
| `NOT_INSTALLED` | 注册表没有 GroupGuardian Handler | 无内容审核联动 |
| `UNVERIFIED` | 无法导入真实 AstrBot 注册表 | 不假设安全，不虚报 |

- 只读接口：`GET /api/v1/plugins/extensions/astrbot_plugin_group_approval/diagnostics`。
- **明确声明**：`VERIFIED` 只代表**当前进程真实注册表**的快照符合预期，不代表已在真实 QQ 环境验证；
  **未打补丁的部署属于不安全部署**，本插件不会谎报为安全。

实现：`approval/diagnostics.py`、`approval/gate.py:15-20`、`patcher` 文件 `patches/`。

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
- **变更操作**：approve/reject/grant/revoke/ban/unban/update_group 等必须携带**纯数字、已核验**的
  `bot_self_id`；缺失、非数字或 `"default"` → 返回 400 且**不写库**（`approval/web_api.py` 参数校验层）。
- **HTTP 语义说明**：AstrBot 网关会在 Handler 之前拒绝未认证请求；插件对“已认证但无权限”的响应是
  **HTTP 200 + body `retcode`（401/403）**，请以 `retcode` 判断，不要仅看 HTTP 状态码。
- 响应中不回显服务器内部异常堆栈；参数校验错误返回结构化错误信息。

---

## 5. Bot 身份与多 Bot 路由

- **平台实例的 `self_id` 不是 Bot QQ 号**（aiocqhttp 的实例/client 标识是连接级 UUID 等）。
- Bot QQ 的合法来源仅两种：
  1. 入站事件 `event.get_self_id()`（由适配器从已认证的 OneBot 连接写入）；
  2. OneBot `get_login_info` 的返回（`approval/napcat_client.py:307-343`）。
- `NapCatClient` 为每个 platform 实例建立“实例 → 已核验 QQ”的绑定；所有 OneBot API 调用显式传
  `self_id=<已核验 QQ>`（`napcat_client.py:499-575`），实现多 Bot 精确路由。
- **未知/歧义身份 → fail closed**（`napcat_client.py:469-497`）：不回退“第一个客户端”，不写
  `"default"` 占位；连接断开时使缓存失效并重新核验。
- 仪表盘 `/bots` 只返回已核验/数据库已知的 Bot 标识，并过滤 `"default"`（`web_api.py:380-398`）。

---

## 6. 通知隐私（Notification privacy）

- **申请人结果通知默认仅私聊**：包含审批备注/拒绝原因的完整内容只发往申请人私聊。
- **群内兜底默认关闭**：仅当 `notifications.user_group_fallback_enabled=true` 时，私聊失败达到重试上限
  才向申请人来源群发送 **`fallback_payload` 短文案**（不含审批理由/备注）；文案由
  `user_group_fallback_text` 配置，缺省为不含敏感内容的最小提示。
- **管理员通知**：优先私聊 Owner/Reviewer；私聊失败仅允许回退到**显式配置的专用审核群**
  （`notifications.fallback_group_id`），**绝不**使用申请来源群。
- **事件去重**：`event_key` 使用稳定业务标识（申请编号 / 用户结果类型+申请编号），入队
  `ON CONFLICT DO NOTHING`；因此重试不会重复入队。
- **投递语义**：outbox 为 **at-least-once**（QQ/OneBot 无 exactly-once 原语）；worker 以事务原子
  claim（`SENDING` + `claimed_at`），超时可回收；最终失败行保留 `last_error`，需人工排查。
  **不宣称 exactly-once 投递。**

实现：`approval/notification_service.py`、`approval/migrations.py`（V2 唯一索引、V6 `fallback_payload`/`claimed_at`）。

---

## 7. 数据库事务与并发安全

- **协程隔离的事务**：每个 `transaction()` 使用**独立 SQLite 连接** + `BEGIN IMMEDIATE`，并在
  `contextvars` 中登记事务连接，使同一 Task 内的数据库调用自动路由到该连接；其他协程使用共享的
  autocommit 连接（`isolation_level=None`），不会混入他人事务（`approval/database.py:1-238`）。
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

## 8. LivingMemory 限制（如实记录，不宣称“记忆隔离”）

- LivingMemory 通过自定义 filter 在 AstrBot **`WakingCheckStage`**（任何消息 Handler 执行之前）调度
  detached task 落库，且**不检查** `event.stop_event()/is_stopped()`。
- 因此：`event.stop_event()` **不能**阻止 LivingMemory 存储未授权用户消息；LLM 相关的 recall/reflection
  对已停止事件不运行，但已存储消息可能在后续 LLM 回合（或管理员 `/lmem summarize`）被汇总进长期记忆。
- 本插件**没有**官方插件 API 可阻止某用户被采集；缓解只能在其配置侧：
  `session_manager.enable_full_group_capture=false`、`access_control.whitelist_enabled + allowed_ids`
  等（详见 `COMPATIBILITY.md` §6；结论来自上游 LivingMemory 2.10.0 源码静态分析）。
- **未实测**：上述为上游源码静态分析结论，未在真实 LivingMemory 运行环境中验证。

---

## 9. 状态与未验证项

| 项目 | 状态 |
|---|---|
| 单元/服务级安全回归（事务、授权状态机、通知隐私、参数校验、身份 fail-closed、迁移） | **已实测**（117 passed, 1 skipped） |
| 真实 AstrBot 4.29.4 注册表/优先级/事件对象 | **已实测**（框架测试 9 passed） |
| 真实 NapCat / 真实 QQ 群 / Windows 生产环境 | **未实测** |
| GroupGuardian 真实安装+补丁联调 | **未实测**（补丁契约测试 + 真实注册表顺序验证） |
| LivingMemory 真实运行行为 | **未实测 / 无法验证** |
| WebUI 真实浏览器桥接 | **未实测**（仅 `node --check` 语法检查） |
| GitHub Actions CI | **未实测**（工作流已配置，未在 GitHub 运行） |

> 任何“已在真实环境验证”的表述都必须等真实 Windows + NapCat + QQ 群联调完成后才能使用。
