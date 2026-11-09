# AstrBot Group Approval (群等级准入与管理员审批)

[![Tests](https://github.com/Maxwell233/astrbot_plugin_group_approval/actions/workflows/tests.yml/badge.svg)](https://github.com/Maxwell233/astrbot_plugin_group_approval/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python: 3.12+](https://img.shields.io/badge/Python-3.12+-green.svg)](https://www.python.org/)
[![AstrBot: v4.26+](https://img.shields.io/badge/AstrBot-v4.26%2B-orange.svg)](https://github.com/AstrBotDevs/AstrBot)

专门为 **AstrBot** 设计的高可靠性 QQ 群权限与安全中间件插件。基于 **NapCat OneBot v11** 获取的群成员真实等级（`memberRealLevel`），实现严格的 **“群等级达标 ➔ 提交使用申请 ➔ 管理员审批 ➔ 获得 Bot 使用权 ➔ 持续复核与自动冻结”** 完整授权生命周期。

> [!IMPORTANT]
> **版本声明：当前版本为 `0.1.1`（开发调试版，非稳定版）**
> 本项目处于早期开发与调试阶段，**不发布生产稳定版 Release**。自动化测试覆盖单元/服务级与“真实 AstrBot 4.29.4 框架注册表”两级；
> **Windows Launcher、NapCat 与真实 QQ 群联调尚未验收**，相关能力统一标注为「未实测」。完整缺陷与修复记录见
> [docs/DEFECT_MATRIX.md](docs/DEFECT_MATRIX.md)。

---

## 项目背景

这个插件是为在规模较大的 QQ 群里使用 AstrBot 而开发的。群成员多，如果人人都能直接触发 AI 对话和插件工具，刷指令和重复调用容易占用 Bot 的资源，也会打扰正常群聊。

为减少滥用，成员达到设定的群内活跃等级后，可以提交使用申请，经管理员批准后获得该群的 Bot 使用权限。审批管的是谁能使用 AI 对话、普通指令和插件工具。群内容审核独立配置，可以覆盖全体成员，包括尚未获批的人。

---

## 🌟 核心特性

- 🎯 **真实群等级准入**：严格提取 NapCat `level`（群成员活跃等级），绝不与 QQ 账号等级（`qq_level`）混淆；支持 `force_refresh` 穿透与并发单飞（single-flight）合并。
- 🛡️ **Fail-Closed 故障阻断**：网络异常、退群、等级缺失、解析错误或 **无法核验 Bot 身份** 时，默认拒绝，而不是回退到第一个实例或假占位符。
- ⚖️ **角色与权限隔离**：
  - **Owner**：最高管理员（QQ 号列表），负责全局群策略、审核员增删、黑名单与手动授权。
  - 审核详情、审批、拒绝、手动授权及审核员管理在当前 Bot 私聊中执行。

**Reviewer**：审核员，负责日常审批、驳回与撤销。
  - **Approved User**：获批群成员，普通业务可用，绝不继承系统管理特权。
  - **QQ 群管隔离**：QQ 群主/群管理员默认不自动继承 Bot 管理特权（`group_admin_bypass` 可按群开启）。
- 🔒 **内容审核优先（GroupGuardian）**：准入闸门固定 `priority = 50`；**必须**给上游 GroupGuardian 主审核 Handler 应用
  `patches/group_guardian_priority.patch`（提升到 `priority = 100`）才能保证“审核先于准入”。未打补丁时插件**明确判定为 CONFLICT，不保证**未授权用户消息被审核，详见
  [COMPATIBILITY.md §5](COMPATIBILITY.md)。
- 🧠 **LivingMemory 被动采集**：关闭原版全群采集，或手动应用兼容补丁，让 AIOCQHTTP 消息在准入通过后才安排采集。见 [补丁说明](patches/README.md)。
- 💾 **SQLite 事务安全**：每个事务使用独立连接 + `contextvars` 协程级事务路由（WAL 模式），审批在**单个原子事务**内完成
  “申请状态 + 授权 + 审计 + 通知入队”；授权状态迁移使用 `revision` 乐观锁。通知队列为 **at-least-once + 入队去重**，不宣称 exactly-once。
- 🔔 **持久化发信队列**：通知写入 `notification_outbox`，支持原子 claim、崩溃恢复、指数退避；申请人结果默认仅私聊发送。
- 🖥️ **现代化 WebUI**：AstrBot Plugin View Bridge（`window.AstrBotPluginView.apiGet/apiPost`），支持多条件检索、审批、手动授权、拉黑、审计导出（真实 Blob 文件下载）。

---

## 📋 运行环境要求

- **操作系统**：Windows 10/11 64 位（亦兼容 macOS / Linux）
- **Python 环境**：Python 3.12+
- **协议端**：[NapCatQQ](https://github.com/NapNeko/NapCatQQ)（OneBot v11 反向 WebSocket）
- **主框架**：[AstrBot](https://github.com/AstrBotDevs/AstrBot) **>= 4.26.0**（由 AstrBot Launcher 启动）
  - `astrbot.api.web`（`request` / `json_response`）在 4.26.0 之前不存在；
  - `event.stop_event()` 在 4.25.0 之前不会阻止后续插件 Handler；
  - 因此旧文档中的 `>=4.16.0` 是错误声明，已修正为 `>=4.26.0`（`metadata.yaml` 同步）。
- **数据库**：内置 SQLite 3（WAL 模式，无需额外数据库）

---

## 🚀 安装部署方法

1. 暂停测试 Bot 的群消息接入。备份现有 SQLite 数据库、插件配置和第三方目标文件。
2. 在 AstrBot 插件目录检出 PR #1 的 `fix/0.1.1-security-integration` 分支。已有安装应沿用原插件目录和数据目录。新安装示例：

   ```powershell
   git clone --branch fix/0.1.1-security-integration https://github.com/Maxwell233/astrbot_plugin_group_approval.git astrbot_plugin_group_approval
   ```

3. 配置 Owner QQ 和群策略，保留 AIOCQHTTP 配置。使用 GroupGuardian 时应用 [主审核补丁](patches/README.md#groupguardian-主审核优先级)，重载后在 Dashboard 诊断确认主审核 100 在准入 50 之前。出现 `CONFLICT`、`DISABLED` 或 `UNVERIFIED` 时，先排除问题再恢复受管群接入。
4. 使用 LivingMemory 时关闭 `session_manager.enable_full_group_capture`，或在测试群验证 [被动采集补丁](patches/README.md#livingmemory-被动采集)。确认普通用户未获授权时不会触发采集或业务插件。
5. 在测试群确认审核、申请、拒绝、授权、通知和多账号路由。核对受管会话同时启用两个审核插件，Bot 具备必要的 QQ 群管理权限，普通业务插件排在准入之后。完整验收项见 [测试报告](INTEGRATION_TEST_REPORT.md)。

---

## 📖 使用与审批全流程

```text
群成员发送「/申请使用」
   ↓
检查目标群是否开启准入 / 黑名单 / 已有授权 / 冷却与限频
   ↓
调用 NapCat 拉取真实群等级（no_cache）
   ↓
【未达门槛】返回当前等级与门槛要求，不入库
【满足门槛】入库 PENDING 申请单，向 Owner/Reviewer 私聊提醒
   ↓
管理员群内/私聊发送 /ga approve <编号>，或登录 WebUI 审批
   ↓
单个 SQLite 原子事务：申请 APPROVED + 授权 ACTIVE + 审计 + 通知入队
   ↓
系统向用户私聊发送结果；用户获得该群 Bot 使用权限
```

---

## ⌨️ QQ 指令清单

### 用户准入指令（无需预先授权即可使用，仅群聊）

| 指令 | 描述 |
|---|---|
| `/申请使用 [理由]` | 提交当前群的 Bot 使用资格申请 |
| `/我的权限` | 查看自己在当前群的授权状态、到期时间与最后核验等级 |
| `/申请状态` | 查询自己的待审申请进度 |
| `/取消申请` | 取消自己提交的待审核申请 |
| `/使用规则` | 查看当前群的最低申请门槛、审批要求与有效期 |

> 准入指令由 `priority=50` 的可信闸门**精确 token 匹配**并在本 Handler 内执行后 `stop_event()`，不会转发给低优先级插件或 LLM。

### 管理员指令（`/ga ...`）

审核详情、审批、拒绝、手动授权及审核员管理在当前 Bot 私聊中执行。

**Reviewer**：`/ga pending [群号]`、`/ga approve <编号> [备注]`、`/ga reject <编号> [原因]`、`/ga revoke <群号> <QQ> [原因]`、`/ga user <群号> <QQ>`、`/ga list <群号>`、`/ga history <编号>`

**Owner 专属**：`/ga grant <群号> <QQ> [天数]`、`/ga ban <群号> <QQ> [原因] [天数]`、`/ga unban <群号> <QQ>`、`/ga reviewer add|remove <QQ>`、`/ga level <群号> <数字>`、`/ga enable|disable <群号>`、`/ga status`

---

## 🖥️ WebUI 管理看板

前端位于 **`views/dashboard/`（唯一源码目录；`pages/dashboard/` 已删除）**，AstrBot 4.29.4 按
`PLUGIN_PAGE_ROOT_DIR_NAMES=("views","pages")` **优先 views**。前端通过 Plugin View Bridge
`window.AstrBotPluginView.apiGet/apiPost` 调用，HTTP 回退路径为
`/api/v1/plugins/extensions/astrbot_plugin_group_approval/...`；**旧 `/api/plug/...` 在 4.26+ 已不存在**。

看板功能：运行概览、申请检索/审批、用户与授权、群策略、审计日志（JSON/CSV 真实文件导出）。
后端所有变更操作在服务端鉴权：只有 AstrBot 的唯一 Dashboard 账号（`dashboard.username`）被授予插件
OWNER。写请求缺少有效格式的 `bot_self_id` 返回 400，指定 Bot 未通过当前 OneBot 连接核验返回 403。
申请与授权操作绑定记录所属 Bot。选择 Bot 后，概览计数与最近申请都按该 Bot 过滤。

---

## ⚙️ 配置

配置项说明见 [docs/CONFIGURATION.md](docs/CONFIGURATION.md)。要点：

- 仅使用 `_conf_schema.json` 中真实生效的配置项；已移除无法兑现的 `application.duplicate_pending_allowed`、
  `security.fail_closed`、`security.gate_priority` 暴露项。
- `security.keep_audit_days` 在**插件启动时**清理过期审计日志（不是后台周期任务）。
- 申请人审批结果默认**仅私聊**发送；群内兜底默认关闭，开启后只发送不含审批理由的最小文案。

---

## 🛡️ 与第三方插件的兼容协同机制

### GroupGuardian

Group Approval 只读取当前 AstrBot 注册表。`VERIFIED` 表示主审核入口的过滤器、启用状态、优先级与排序符合预期。实际审核还取决于每群配置、会话插件过滤、QQ 群权限和审核服务可用性。

### LivingMemory 和其他前置路径

原版 LivingMemory 在 AstrBot 的 `WakingCheckStage` 启动被动采集，早于 `stop_event()`。兼容补丁覆盖该入口。AstrBot 的 `SessionWaiter`、其他插件有副作用的过滤器和独立后台任务另行受其各自的权限检查约束。见 [兼容性边界](COMPATIBILITY.md)。

---

## 自动化测试

本地运行 Python 3.12、3.13 单元和服务测试，另在 AstrBot 4.29.4 中运行真实处理阶段与 aiocqhttp 组件测试。报告记录测试结果和模拟边界。远程状态以 PR 当前提交的检查为准。 [测试报告](INTEGRATION_TEST_REPORT.md) · [PR #1](https://github.com/Maxwell233/astrbot_plugin_group_approval/pull/1)

---

## 💾 数据备份建议

插件数据库默认位于 AstrBot 数据目录下的插件数据目录：

```text
<AstrBot根目录>/data/plugin_data/astrbot_plugin_group_approval/group_approval.db
```

（AstrBot 4.29.4 的 `StarTools.get_data_dir()` 返回 `data/plugin_data/{plugin_name}`；实际路径以运行日志/目录为准。）
该文件在插件代码更新或 Git 拉取时不会被覆盖；可使用标准 SQLite 在线备份方式备份。

---

## 📚 文档索引

- [COMPATIBILITY.md](COMPATIBILITY.md) — 版本要求、Handler 优先级、`stop_event` 边界、GroupGuardian/LivingMemory 事实
- [INTEGRATION_TEST_REPORT.md](INTEGRATION_TEST_REPORT.md) — 测试分级与实测/未实测声明
- [docs/DEFECT_MATRIX.md](docs/DEFECT_MATRIX.md) — 缺陷矩阵、修复设计与回归测试证据
- [docs/SECURITY.md](docs/SECURITY.md) — 安全边界、鉴权、通知隐私、事务模型
- [docs/CONFIGURATION.md](docs/CONFIGURATION.md) — 配置参数说明
- [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) — 常见问题排查

---

## 📄 开源许可证

本项目采用 [MIT 许可证](LICENSE) 开源。欢迎提交 Pull Request 与 Issue。
