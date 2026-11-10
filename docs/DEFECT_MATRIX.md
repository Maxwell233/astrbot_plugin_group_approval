# 缺陷矩阵（Defect Matrix）— v0.1.1 审计与修复记录

本文记录 0.1.1 的历史审计。当前双域架构和测试见 [0.1.2 验证报告](../INTEGRATION_TEST_REPORT.md)。

> 本文记录 0.1.1 的历史修复。0.1.2 的准入范围、测试与第三方兼容方式见 [新版验证报告](../INTEGRATION_TEST_REPORT.md)。

以下为历史审计记录，基线为 PR #1 head `eba75d8feda2c4513ec7b12136f471ec059d6218`。当前修复和验证见 [本轮测试报告](../INTEGRATION_TEST_REPORT.md)。
修复分支：`fix/0.1.1-security-integration`。版本保持 `0.1.1`，Development / 开发调试版。

图例：
- **验证状态**：`已通过` = 有回归测试且旧代码可复现失败 / 新代码通过；`框架级已通过` = 真实 AstrBot 4.29.4 注册表/事件测试通过；`未实测` = 需要真实 NapCat + QQ 群，当前环境不具备；`文档限制` = 框架层面无法由本插件修复，已如实记录。

| 编号 | 严重程度 | 文件:行 | 根本原因 | 修复设计 | 回归测试 | 验证状态 |
|---|---|---|---|---|---|---|
| BUG-001 | P0 | `main.py:439`（旧） | 准入闸门 `priority=50` 早于 GroupGuardian 默认 `priority=0` 执行，`stop_event()` 使审核 Handler 完全不运行 | 提供并强制部署 GroupGuardian 补丁（`patches/group_guardian_priority.patch`，主审核 Handler→100）；`HandlerDiagnostics` 读取真实 `star_handlers_registry` 报告 VERIFIED/CONFLICT/NOT_INSTALLED/UNVERIFIED；启动时记录真实状态，不再运行时篡改第三方 Handler | `tests/test_framework_handler_order.py`（真实 AstrBot 注册表：100→50→0 顺序；stock GG=0 被闸门抢占的复现） | 框架级已通过；未打补丁的部署被明确判定为不安全 |
| BUG-002 | P0 | `approval/database.py:20-23,53-67,114-119`（旧） | 单一共享连接 + 进程级 `_in_transaction` 布尔值：协程 B 的写入进入 A 的事务，被 A 回滚/提前提交，读取可见未提交数据 | 每个事务使用独立连接 + `BEGIN IMMEDIATE`/显式提交回滚；`contextvars` 任务级事务上下文路由；共享连接 `isolation_level=None` 自动提交；`commit()/rollback()` 永不结束他人事务；事务可重入；WAL；事务内禁止网络 I/O | `tests/test_db_transaction_isolation.py`（6 项：未提交不可见、回滚不吞他人写入、tx 内 commit 不提前结束、嵌套、并发串行、失败整体回滚） | 已通过（旧代码：3 项失败 + 嵌套事务死锁；新代码 6 项通过） |
| BUG-003 | P0 | `approval/approval_service.py:378-520`（旧） | 审批的状态校验/过期校验在事务外，`UPDATE` 未校验 rowcount，审计与 outbox 与申请更新不原子 | 审批改为单事务：`BEGIN IMMEDIATE` 内重读校验状态/过期 → 条件 `UPDATE ... AND status='PENDING'` 且校验 rowcount → grant upsert → 审计 → outbox 入队；网络等级查询在事务外；失败整体回滚 | `tests/test_v011_round2.py::test_atomic_approval_rollback_on_failure`、`tests/test_db_transaction_isolation.py` | 已通过 |
| BUG-004 | P0 | `approval/access_service.py:216-225`（旧） | 等级下降自动暂停的 `UPDATE` 未校验 `status='ACTIVE'`：并发管理员撤销后，REVOKED 被改成 SUSPENDED，随后可能被自动恢复为 ACTIVE | grants 增加 `revision` 乐观锁（Migration V4）；所有自动/手动状态转换使用 `WHERE status=? AND revision=?` 条件更新并校验 rowcount；撤销/封禁优先级最高；自动恢复仅限 SUSPENDED | `tests/test_grant_state_race.py`（撤权 vs 等级检查、封禁 vs 等级检查、过期不可复活、并发限频） | 已通过（旧代码 4 项全部失败，含新代码通过后仍 REVOKED 的断言） |
| BUG-005 | P0 | `approval/gate.py:37-43,93-94`（旧） | 准入指令用 `startswith`/裸 `"ga"` 模糊匹配并“放行给低优先级 Handler”，未授权用户可借此触达其他业务插件 | 闸门成为唯一可信准入分发器：按完整命令 token 精确匹配（`申请使用/我的权限/申请状态/取消申请/使用规则/ga`），尊重 AstrBot `is_at_or_wake_command`（全局 wake_prefix）；命中后在本 Handler 内执行并 `stop_event()`；非聊天事件（notice/request）显式跳过 | `tests/test_integration.py::test_admission_commands_are_classified_for_local_dispatch`、`test_unauthorized_user_blocked_from_ai_and_plugins`；`tests/test_framework_handler_order.py`（真实事件端到端执行并 stop） | 已通过 + 框架级已通过 |
| BUG-006 | P1 | `approval/napcat_client.py:177-191`（旧） | 依赖不存在的 `client.self_id`/`inst.self_id`；`call_action` 未传 `self_id`；未知 Bot 回退第一个实例 | 仅官方 API：`platform_manager.get_insts()`/`meta().name=="aiocqhttp"`/`get_client()`/`call_action`；`get_login_info` 验证真实 QQ 并按 platform 实例建映射；所有调用传 `self_id=`；未知/歧义身份 fail closed；断线/异常失效重建；`remember_event_identity` 用入站事件自证 | `tests/test_bot_identity.py`（22 项：完整/已解析/错误响应、断线重连、双 Bot、身份不匹配、回执异常、超时重试、fail closed） | 已通过（真实 NapCat 连接未实测） |
| BUG-007 | P1 | `approval/auth_service.py:57-58,61-69`（旧） | 把 Dashboard 用户名 `admin/root/owner` 硬编码为 OWNER；把 QQ Owner/Reviewer 配置用于 Dashboard 身份 | 仅使用 AstrBot 注入的 `request.username`；仅当等于 `dashboard.username` 时为 OWNER；`api_key:` 机器身份拒绝；QQ 身份与 Dashboard 身份彻底分离；未知身份管理操作一律拒绝 | `tests/test_v011_features.py::test_web_api_v011_features`、`tests/test_v011_round2.py::test_webui_auth_enforcement` | 已通过 |
| BUG-008 | P1 | `approval/web_api.py:272,304,332,360,391`（旧） | 业务写路径用 `bot_self_id="default"` 占位；列表过滤同时隐藏该占位，形成隐藏授权命名空间 | 全面移除 `"default"`：缺失/伪造 Bot 身份的管理操作返回 400 且不写库；列表为空表示不过滤；`list_bots` 仅返回已验证 Bot | `tests/test_web_api_params.py`（参数校验与 default 拒绝） | 已通过（见实施后测试） |
| BUG-009 | P1 | `approval/notification_service.py:135,140,197-215`（旧） | 用户审批通知 `fallback_group=group_id`，私聊失败后把审批理由/备注发到公开群；`event_key` 用秒级时间戳可能丢通知 | 用户通知默认仅私聊；群兜底需显式开启且只发不含敏感内容的 `fallback_payload`；管理员兜底仅发往专用审核群；`event_key` 改为稳定业务 ID（申请编号）；outbox 原子 claim（SENDING+claimed_at）、崩溃恢复、指数退避、真实发送结果与错误留存 | `tests/test_notification_privacy.py`（6 项：默认不泄漏、显式开启只发最小文案、幂等、管理员专用群、并发不重复发送、私聊成功路径） | 已通过 |
| BUG-010 | P1 | `approval/approval_service.py:264-270,838-866`（旧） | 限频“读-改-写”非原子，并发申请可突破上限 | 限频检查+自增放入 `BEGIN IMMEDIATE` 事务（可重入），并与申请创建同事务 | `tests/test_grant_state_race.py::test_concurrent_rate_limit_check_is_atomic` | 已通过（旧代码 10/10 通过，新代码恰好 3 次） |
| BUG-011 | P1 | `approval/approval_service.py:885-921`（旧） | 删除 Reviewer 只改内存，重启后从旧配置重新出现 | Migration V5 `reviewer_overrides` 持久记录 ADDED/REMOVED；REMOVED 优先于配置种子；启动同步时剔除 | `tests/test_v011_features.py::test_reviewer_persistence_in_db`（+ 新覆盖） | 已通过 |
| BUG-012 | P1 | `approval/access_service.py:196`（旧） | `is_level_exempt` 同时豁免了群成员身份复核，退群用户仍保留权限 | 等级豁免仅豁免最低等级阈值；成员身份复核对所有授权执行；已过期不可恢复 | `tests/test_grant_state_race.py::test_expired_grant_cannot_be_revived`（+ 成员复核断言） | 已通过 |
| BUG-013 | P1 | `approval/migrations.py:143`（旧） | V2 直接建 `UNIQUE INDEX`，旧库有重复 `event_key` 时迁移中止 | 先按 `event_key` 去重再建唯一索引；新增 V4/V5/V6 均为增量、可重复执行；保留 V1–V3 数据 | `tests/test_migrations.py`（旧库升级保留数据、重复 key 去重、幂等、无 placeholder bot id） | 已通过 |
| BUG-014 | P1 | `metadata.yaml:18`（旧） | 声明 `astrbot_version >=4.16.0`，但 `astrbot.api.web` 在 <4.26.0 不存在，`stop_event()` 在 <4.25.0 不阻断后续 Handler | 最低版本修正为 `>=4.26.0`；文档与 CI 同步 | `tests/test_framework_handler_order.py`（真实 4.29.4）；版本边界见 `COMPATIBILITY.md` | 已修复并文档化 |
| BUG-015 | P1 | `main.py:232`（旧） | `__init__` 中 `asyncio.create_task` 做初始化，Handler 可能先于 DB schema 就绪；`terminate()` 不取消该任务；Web 路由在 `__init__` 注册 | 启动迁移到 AstrBot 官方 `async def initialize()`（被 await）；`terminate()` 停止 worker 并关库；无 fire-and-forget 任务 | `tests/test_framework_handler_order.py::test_real_gate_*`（真实 AstrBot 下 `initialize()`/`terminate()`） | 框架级已通过 |
| BUG-016 | P2 | `approval/diagnostics.py:30-46`（旧） | 通过不存在的 `context.star_manager/star_handlers` 枚举，永远 0 Handler，安全诊断是死代码 | 直接导入真实 `astrbot.core.star.star_handler.star_handlers_registry`，读取 `extras_configs["priority"]`，按真实顺序报告 | `tests/test_v011_features.py::test_handler_diagnostics_service`（stock=CONFLICT / patched=VERIFIED / missing / unverified）+ 真实注册表测试 | 已通过 + 框架级已通过 |
| BUG-017 | P2 | `pages/dashboard/` + `views/dashboard/` | 双份前端源码分叉 | 以 AstrBot 4.29.4 优先目录 `views/` 为唯一源码（`PLUGIN_PAGE_ROOT_DIR_NAMES=("views","pages")`，views 优先），删除 `pages/` 副本；统一走 Plugin View Bridge | 前端语法检查 `node --check`（见 INTEGRATION_TEST_REPORT） | 已修复（真实浏览器未实测） |
| BUG-018 | P2 | `docs/*`、`README.md`、`INTEGRATION_TEST_REPORT.md` | 文档声称的测试数量、优先级、LivingMemory 隔离与真实代码不符 | 文档按代码事实重写；明确区分单元/服务级/真实框架/真实 QQ 测试；LivingMemory 限制如实记录 | 文档审查（本矩阵与 INTEGRATION_TEST_REPORT） | 已修复 |
| LIMIT-001 | — | LivingMemory `passive_group_capture` | LivingMemory 在 AstrBot `WakingCheckStage` 的 filter 副作用中调度独立任务落库，早于任何 Handler 且不检查 `is_stopped()`，`stop_event()` 无法阻止采集 | 插件 API 无法阻断；如实记录并提供配置级缓解（`enable_full_group_capture=false` / `access_control.allowed_ids`），不宣称已隔离 | 静态源码证据（`audit/thirdparty-facts.md` §2），未运行 LivingMemory | 文档限制；未实测 |

## 旧代码复现证据（摘要）

| 缺陷 | 旧代码结果 | 新代码结果 |
|---|---|---|
| BUG-002 事务隔离 | `FFF` + 嵌套事务死锁（测试超时） | 6 passed |
| BUG-004 撤权竞态 | 4 failed（REVOKED 被改成 SUSPENDED / 过期授权可撤销 / 限频 10/10） | 4 passed |
| BUG-001 审核顺序 | 真实注册表：gate(50) 先于 stock GG(0)，GG 未运行 | patched GG(100)→gate(50)→business(0)，business 永不运行 |

## 未覆盖 / 无法验证

- 真实 NapCat 连接、真实 QQ 测试群、真实 GroupGuardian 打补丁后的联调、真实 LivingMemory 运行：**未实测**（环境不具备）。
- GroupGuardian 补丁仅通过源码级验证（对上游克隆应用后检查装饰器与真实注册表顺序）；未向上游仓库提交 PR。
- “QQ 消息严格 Exactly Once”不成立：outbox 为 at-least-once + 入队去重。
