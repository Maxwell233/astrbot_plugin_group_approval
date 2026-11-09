# 安全与边界防御设计 (SECURITY.md)

本插件是 AstrBot 生态中负责群准入防护的核心安全中间件，遵循纵深防御与最小特权原则。

---

## 1. 身份与鉴权原则

- **三元身份组**：系统以 `(bot_self_id, group_id, user_id)` 作为不可伪造的授权主键。
- **纯数字 ID 鉴权**：系统严格读取底层协议事件的 `sender.user_id` 和 `message_obj.group_id`，杜绝任何根据群名片、用户昵称、转发消息伪造名片或 LLM 意图推导进行权限判断。
- **角色特权分离**：
  - **Owner**：拥有管理审核员、修改门槛、配置策略与手动授权的最高权限。
  - **Reviewer**：仅拥有审批、拒绝、撤销授权的日常工作权限，无权篡改全局安全策略或赋予自己系统权限。
  - **Approved User**：普通获批用户，**绝不会**自动继承 AstrBot 系统管理员权限。
  - **QQ 群管理员非 Bot 管理员**：QQ 群主与群管理员默认不继承 Bot 的管理特权，避免群管越权操控 Bot。

---

## 2. 故障阻断模式 (Fail-Closed)

在涉及权限判断或等级查询的关键节点，若遇到以下情况：
- 网络请求超时（如 NapCat 查询超 5 秒）
- 协议端断开连接
- 目标群成员已退群或被移出群聊
- 返回数据格式异常、等级为空或无法解析

系统一律执行 **Fail-Closed（严格阻断）**，绝对不会因系统异常或报错而意外放行普通业务或产生非法授权。

---

## 3. GroupGuardian 兼容与审核顺序保障

- **核心目标**：内容安全审核必须覆盖全群，绝不可被准入插件中断。
- **AstrBot 优先级机制深度剖析**：
  - AstrBot 在插件加载注册阶段通过 Handler 的 `extras_configs["priority"]` 进行排序。如果在运行时动态修改 Handler 实例上的属性（如 `handler.priority = 100`），由于内部事件分发链已在启动阶段按降序排定，该修改**无法改变**实际执行顺序。
  - 因此，Group Approval 坚决移除了无效的运行时 monkey-patch 逻辑，采用**标准上游补丁声明 + 运行期只读诊断警告**的双轨防御方案。
- **推荐上游补丁 (zcj-ui/astrbot_plugin_group_guardian)**：
  在 GroupGuardian 主审核 Handler 的过滤器装饰器中显式指定 `priority=100`：
  ```python
  @filter.event_message_type(
      filter.EventMessageType.ALL,
      priority=100,
  )
  async def audit_group_message(self, event: AstrMessageEvent):
      # 内容审核逻辑
  ```
- **运行期 Handler 诊断服务 (HandlerDiagnostics)**：
  Group Approval 内置只读诊断服务，通过检查 AstrBot 注册表中的 `extras_configs["priority"]`，实时评估 GroupGuardian 与 Group Approval (priority=50) 的相对优先次序：
  - 若 GroupGuardian 优先级 $\ge 100$：诊断状态为 `OPTIMAL`（顺序安全）。
  - 若 GroupGuardian 优先级 $\le 50$：诊断状态为 `WARNING`，并在 WebUI 显著标黄告警，提示管理员安装补丁版本。
  - 若未检测到 GroupGuardian：诊断状态为 `NOT_INSTALLED`。
- **执行时序保证**：
  未获批成员发送的所有消息（包括文字、图片、表情）会最先送达 GroupGuardian 进行安全与合规检测，若触发违规行为立即执行撤回、禁言或告警。随后才由 Group Approval 在 priority=50 拦截并阻止其调用 Bot 业务。未授权用户的消息绝不会穿透至普通插件。

---

## 4. WebUI 身份认证与服务端授权防御

- **杜绝伪造 HTTP 标头**：
  - 严禁信任客户端传入的 `X-AstrBot-User`、`X-User-Id` 或 `Authorization-User` 请求头。任何网络调用方均可随意构造此类标头伪造管理员身份。
  - Group Approval 仅读取 Quart / AstrBot 官方 Dashboard 经过校验的上下文（`request.username` 或服务端 session）。
  - 未经身份验证的请求一律由网关层直接拒绝并返回 HTTP 401 Unauthorized。
- **服务端强授权校验**：
  - 严禁信任前端或客户端 JSON 传入的 `actor_role: "OWNER"`。
  - 系统由 `AuthorizationService` 在服务端统一将当前登录账号与数据库审核员名单及配置文件 Owner 列表做权限比对，动态赋予 `OWNER`、`REVIEWER` 或 `DASHBOARD_USER` 角色。
  - 特权操作（如免等级特权授权、黑名单操作、策略变更、日志导出）严格限定只有真实 `OWNER` 方可执行；`REVIEWER` 仅允许执行日常审批与拒绝。
- **多 Bot 隔离与路由**：
  - 杜绝 `"default"` 占位符退化。所有群策略、授权凭证、通知出件箱严格携带真实的 `bot_self_id`。
  - 跨 Bot 实例间策略与授权彻底隔离，杜绝多账号环境下的越权访问。

---

## 4. LivingMemory 长期记忆隔离

- **拦截时机**：Group Approval 通过 `event.stop_event()` 阻断未获批用户的消息流向 AstrBot Agent。
- **记忆保护**：因为消息在消息管道入口即被阻断，下游的 `@filter.on_llm_request` 与主动会话反思钩子均不会被唤起，确保未授权成员的群聊碎语绝对不会被向量化提取为持久化用户画像或记忆库。

---

## 5. 并发与事务安全

- **SQLite 事务保障**：所有申请状态变更、授权发放与撤销均在 `aiosqlite` 显式事务中执行。
- **防重复审批**：并发审批同一份申请时，数据库行锁与 `status = 'PENDING'` 前置条件确保仅有且必须只有一个事务执行成功，其余请求直接返回明确冲突提示并记录审计追踪。
