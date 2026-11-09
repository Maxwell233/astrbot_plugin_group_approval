# 集成测试与验证报告 (INTEGRATION_TEST_REPORT.md)

**当前版本：** `0.1.1`（早期开发与调试阶段，非生产正式版）  
**测试时间：** 2026-11-09  
**测试环境：** macOS / Python 3.12.16 / pytest 9.2.0 / pytest-asyncio 1.4.1 / ruff 0.17.4  
**目标部署平台：** Windows 10/11, AstrBot Launcher, NapCat OneBot v11, SQLite WAL  

> [!WARNING]
> **版本与调试阶段说明**：本项目目前仍处于**早期开发与调试阶段**，尚未达到生产环境的全面稳定运行标准。本报告记录代码在模拟协议端与自动化测试套件下的验证结果。所有未在真实 Windows + NapCatQQ + 真实 QQ 群中实操的功能均标注为「待实测」，代码推送 GitHub 供持续集成与开发调试，**暂不发布正式稳定版 Release**。  

---

## 一、参照与实测系统版本

| 组件 | 参照与测试版本 / Commit | 状态 | 说明 |
|---|---|---|---|
| **AstrBot** | v4.16.0+ (Star v2 规范) | 模拟协议与适配层测试通过 | 原生事件、Star 插件生命周期及 Web API 适配完毕 |
| **NapCatQQ** | OneBot v11 标准反向 WebSocket | 适配层接口测试通过 | `get_group_member_info`、`no_cache`、`send_private_msg` |
| **GroupGuardian** | 最新版 (`zcj-ui/astrbot_plugin_group_guardian`) | 时序与管道拦截验证通过 | priority=100 先行完成内容安全审核 |
| **LivingMemory** | v2.9.1+ (`lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory`) | 记忆隔离机制验证通过 | priority=50 阻断 LLM 管道，防止未授权向量化 |
| **Jrys / Rollpig / Cyber Divination** | 标准 Star 插件 | 准入阻断测试通过 | 无论是命令前缀还是无前缀短语均无法越权穿透 |

---

## 二、消息 Handler 实际执行时序验证

通过集成模拟流水线（`tests/test_integration.py`）对消息流经顺序进行严格断言：

```text
[QQ 平台下发群消息]
         ↓
1. GroupGuardian (priority=100)
   • 执行正则过滤、词库匹配、违规图片 OCR
   • 无论消息发送者是否为已获批成员，内容审核均无差别触发
   • 违规行为立即执行撤回、警告或禁言
         ↓ (未违规消息继续传递)
2. Group Approval Gate (priority=50)
   • 10 步准入决策引擎校验
   • 排除 Bot 自身、检查黑名单、Owner/群管豁免
   • 校验授权状态、有效期与等级复核
   • 未通过用户：执行 event.stop_event()，并根据场景发送友好提示或静默阻断
         ↓ (仅允许已获批用户通过)
3. 业务插件与 AstrBot Agent (priority <= 0)
   • 今日运势 (Jrys)
   • 抽小猪 (Rollpig)
   • 赛博占卜 (Cyber Divination)
   • AstrBot LLM Agent 对话
   • LivingMemory (on_llm_request 记忆召回与反思)
```

**断言结论：**
1. 未授权用户的违规消息会被 GroupGuardian 正常审核处置，Group Approval 不会阻断安全审核。
2. 未授权用户的普通消息在 priority=50 被完全截断，绝不会到达 priority=0 的第三方插件或 LLM 管道。

---

## 三、自动化测试执行清单 (43 / 43 PASSED)

所有 43 项测试在本地测试沙箱中全部通过（0.22 秒）：

### 1. 群等级解析与服务 (`test_level_service.py`)
- [x] `test_level_parsing_formats`: 支持整数、"18"、"LV18"、"等级20" 等多种格式严格提取。
- [x] `test_member_level_comparison`: 验证 LV9 申请 LV10 拒绝、LV10 允许、LV25 允许。
- [x] `test_qq_level_vs_group_level`: 严格核验群等级 `level`，绝不混淆 QQ 账号等级 `qq_level`。
- [x] `test_missing_or_invalid_level`: 等级缺失或非法文本判定为 INVALID_LEVEL。
- [x] `test_timeout_handling`: NapCat API 查询超时优雅处理。
- [x] `test_left_group_member`: 成员退群返回 is_member=False。
- [x] `test_identity_mismatch_check`: 返回群号或 QQ 号不匹配时严格拒绝（GROUP_ID_MISMATCH）。
- [x] `test_caching_and_force_refresh`: 验证 300 秒 TTL 缓存命中与 force_refresh=True 穿透。
- [x] `test_concurrent_request_coalescing`: 单飞防抖机制，并发相同用户查询仅向远程发起 1 次真实调用。

### 2. 申请审批流与状态机 (`test_applications.py`)
- [x] `test_apply_qualifying_level_creates_pending`: 达到门槛用户生成 PENDING 申请单。
- [x] `test_apply_under_level_rejected`: 等级未达标者不建立申请记录。
- [x] `test_duplicate_pending_prevented`: 同一用户禁止重复提交未审批申请。
- [x] `test_reviewer_can_approve`: 审核员批准申请，生成 ACTIVE 授权并记录批准人。
- [x] `test_reviewer_can_reject`: 审核员拒绝申请，记录拒绝原因并进入冷却期。
- [x] `test_expired_application_cannot_be_approved`: 过期申请自动标记 EXPIRED，无法被批准。

### 3. 并发安全与事务 (`test_concurrency.py`)
- [x] `test_concurrent_approval_atomic`: 两个管理员并发审批同一申请，SQLite 事务保障只有 1 个成功，第 2 个收到明确已被处理提示，无脏数据。
- [x] `test_concurrent_application_submission`: 并发申请提交去重，保证数据库只有 1 条待审记录。

### 4. 权限决策引擎 (`test_access.py`)
- [x] `test_access_requires_active_grant`: 必须拥有 ACTIVE 授权方可放行。
- [x] `test_revocation_immediately_blocks_access`: 授权撤销后立即阻断访问。
- [x] `test_blacklist_overrides_active_grant`: 黑名单优先级高于活跃授权。
- [x] `test_cross_group_authorization_isolation`: 群 A 获批在群 B 严格无权限，跨群隔离。
- [x] `test_cross_bot_id_isolation`: 不同 Bot QQ 间授权独立不交叉。
- [x] `test_persistence_across_restart`: 重建服务后 SQLite 数据保持有效，重启不丢失状态。
- [x] `test_level_drop_suspends_and_recovery_restores`: 授权成员等级掉出要求自动挂起为 SUSPENDED；等级回升自动恢复 ACTIVE。

### 5. 准入拦截闸门与多插件集成 (`test_integration.py`)
- [x] `test_unauthorized_user_blocked_from_ai_and_plugins`: 未授权用户无法触发 AI 对话、运势、塔罗、小猪等功能。
- [x] `test_group_guardian_audit_precedence`: GroupGuardian (priority=100) 审核领先于 Group Approval (priority=50)。
- [x] `test_living_memory_isolation`: 未授权用户在消息入口被拦截，下游 LLM 不被唤起，无法生成个人长期记忆画像。
- [x] `test_private_chat_and_admin_commands`: 普通用户私聊受限，管理员私聊管理指令正常响应。
- [x] `test_fail_closed_on_napcat_disconnect`: NapCat 网络异常时严格阻断，不会意外放行。

### 6. 控制指令与 WebUI API (`test_commands.py`, `test_web_api.py`)
- [x] `test_user_commands_flow`: 用户指令 `/申请使用`、`/我的权限`、`/申请状态`、`/取消申请`、`/使用规则` 全流程正常。
- [x] `test_admin_commands_permissions`: 普通用户无权执行 `/ga`；Reviewer 无法执行 Owner 专属指令；Owner 指令全功能覆盖。
- [x] `test_web_api_dashboard_and_applications`: WebUI 看板指标、申请分页列表、审批/拒绝 API。
- [x] `test_web_api_group_policy_update`: 群准入策略动态编辑 API。
- [x] `test_web_api_audit_export`: 审计日志检索与 JSON/CSV 格式导出。

### 7. v0.1.1 安全修复与扩展测试 (`test_v011_features.py`)
- [x] `test_napcat_normalization_formats`: 兼容完整 OneBot 包裹响应与已解包字典格式；准确识别网络错误与超时状态码。
- [x] `test_napcat_multi_bot_resolution`: 多 Bot 环境下按 `bot_self_id` 精准寻址目标 NapCat 实例，无串号串群风险。
- [x] `test_transaction_rollback_on_error`: `BEGIN IMMEDIATE` 显式事务，在写入中途发生异常时完整回滚，无残留脏数据。
- [x] `test_level_exemption_lifecycle`: 仅 Owner 可授予 `is_level_exempt=True`；免等级核验用户等级下滑时不被挂起。
- [x] `test_application_rate_limiting`: 滑动窗口申请频次限制，超频自动拦截并友好提示。
- [x] `test_reviewer_persistence_in_db`: 审核员数据持久化落盘至 SQLite `reviewers` 表，重启与重载后不丢失。
- [x] `test_group_unban_does_not_clear_global_ban`: 单群解黑名单绝不误清全局黑名单；全局解黑名单与群黑名单权限隔离。
- [x] `test_web_api_v011_features`: 支持 `/bots` 多实例查询，兼容 `request.query.get` 与 `await request.json(default={})`，提取真实管理员凭证。
- [x] `test_group_guardian_priority_promotion`: 插件初始化时动态扫描并提升 GroupGuardian 至 priority=100，确保全群安全审核优先执行。

---

## 四、真实环境验收状态与待实测说明

按照任务书验收原则，严格区分**自动化测试**与**真实环境验收**：

1. **已完全通过自动化验证项目**：
   - 全套 Python 3.12 单元与集成测试套件（43/43 测试通过）。
   - Ruff 严格静态类型与代码质量检查（0 报错）。
   - SQLite 事务原子性、WAL 模式、外键约束与 Migration V2 迁移。
   - OneBot v11 协议适配与双响应格式标准化处理。
   - WebUI XSS 安全防护与多 Bot 切换 API。
   - GitHub Actions CI 自动化构建配置。

2. **待实机验证清单（Pending Live Verification）**：
   > [!NOTE]
   > 下列项目在模拟端测试通过，但尚未在真实物理生产环境中完成实测验收，使用前需在测试群逐步确认：
   - 真实的 Windows 10/11 生产机上的 AstrBot Launcher 守护进程常驻。
   - 真实的 NapCatQQ 实际二进制在百人以上活跃群中读取 `memberRealLevel` 的延迟与网络抖动。
   - 真实 QQ 客户端私聊防骚扰风控策略对审批通知到达率的实际影响。
   - AstrBot 官方 Web 控制台多用户鉴权 Header (`X-AstrBot-User`) 在真实跨域反向代理下的传递表现。
