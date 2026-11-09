# AstrBot Group Approval (群等级准入与管理员审批)

[![Tests](https://github.com/Maxwell233/astrbot_plugin_group_approval/actions/workflows/tests.yml/badge.svg)](https://github.com/Maxwell233/astrbot_plugin_group_approval/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python: 3.12+](https://img.shields.io/badge/Python-3.12+-green.svg)](https://www.python.org/)
[![AstrBot: v4.26+](https://img.shields.io/badge/AstrBot-v4.26%2B-orange.svg)](https://github.com/AstrBotDevs/AstrBot)

专门为 **AstrBot** 设计的高可靠性 QQ 群权限与安全中间件插件。基于 **NapCat OneBot v11** 获取的群成员真实等级（`memberRealLevel`），实现严格的 **“群等级达标 ➔ 提交使用申请 ➔ 管理员审批 ➔ 获得 Bot 使用权 ➔ 持续复核与自动冻结”** 完整授权生命周期。

> [!IMPORTANT]
> **版本声明：当前版本为 `0.1.1`（开发调试版，非稳定版）**
> 本项目处于早期开发与调试阶段，**不发布生产稳定版 Release**。自动化测试覆盖单元/服务级与“真实 AstrBot 4.29.4 框架注册表”两级；
> **真实 Windows + NapCat + QQ 群环境尚未验收**，相关能力统一标注为「未实测」。完整缺陷与修复记录见
> [docs/DEFECT_MATRIX.md](docs/DEFECT_MATRIX.md)。

---

## 🌟 核心特性

- 🎯 **真实群等级准入**：严格提取 NapCat `level`（群成员活跃等级），绝不与 QQ 账号等级（`qq_level`）混淆；支持 `force_refresh` 穿透与并发单飞（single-flight）合并。
- 🛡️ **Fail-Closed 故障阻断**：网络异常、退群、等级缺失、解析错误或 **无法核验 Bot 身份** 时，默认拒绝，而不是回退到第一个实例或假占位符。
- ⚖️ **角色与权限隔离**：
  - **Owner**：最高管理员（QQ 号列表），负责全局群策略、审核员增删、黑名单与手动授权。
  - **Reviewer**：审核员，负责日常审批、驳回与撤销。
  - **Approved User**：获批群成员，普通业务可用，绝不继承系统管理特权。
  - **QQ 群管隔离**：QQ 群主/群管理员默认不自动继承 Bot 管理特权（`group_admin_bypass` 可按群开启）。
- 🔒 **内容审核优先（GroupGuardian）**：准入闸门固定 `priority = 50`；**必须**给上游 GroupGuardian 主审核 Handler 应用
  `patches/group_guardian_priority.patch`（提升到 `priority = 100`）才能保证“审核先于准入”。未打补丁时插件**明确判定为 CONFLICT，不保证**未授权用户消息被审核，详见
  [COMPATIBILITY.md §5](COMPATIBILITY.md)。
- 🧠 **LivingMemory 限制（如实声明）**：`event.stop_event()` **无法**阻止 LivingMemory 在 AstrBot `WakingCheckStage` 阶段已经调度的群消息采集；本插件不宣称“记忆隔离”，只提供配置级缓解建议（见
  [COMPATIBILITY.md §6](COMPATIBILITY.md)）。
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

### 方式一：AstrBot Launcher / WebUI 扩展商店（推荐）

1. 登录 AstrBot 管理控制台。
2. 导航至 **扩展 -> 插件**，点击 **从 GitHub 仓库安装**。
3. 输入仓库地址：`https://github.com/Maxwell233/astrbot_plugin_group_approval`
4. 安装后，在插件配置中指定 **Owner QQ 号**，重启或热重载 AstrBot。

### 方式二：手动克隆至插件目录

```powershell
cd <AstrBot安装目录>\data\plugins
git clone https://github.com/Maxwell233/astrbot_plugin_group_approval.git astrbot_plugin_group_approval
# 依赖只有 aiosqlite（AstrBot 自带），一般无需额外安装
pip install -r astrbot_plugin_group_approval\requirements.txt
```

### ⚠️ 必须部署步骤：GroupGuardian 兼容补丁

若你使用 [GroupGuardian](https://github.com/zcj-ui/astrbot_plugin_group_guardian) 做群内容审核，**必须**给它的主审核 Handler 打补丁，否则准入闸门（`priority=50`）会先执行 `stop_event()`，导致 GroupGuardian 的审核（上游默认 `priority=0`）**根本不运行**：

```bash
# 在 GroupGuardian 插件目录内执行（先备份 main.py）
git apply --check /path/to/astrbot_plugin_group_approval/patches/group_guardian_priority.patch
git apply         /path/to/astrbot_plugin_group_approval/patches/group_guardian_priority.patch
```

说明与许可证见 [patches/README.md](patches/README.md)。**未打补丁的部署不保证**未授权用户消息会被审核；插件只会在启动日志与 diagnostics 接口中如实报告 `CONFLICT`。

### LivingMemory（可选，风险自担）

LivingMemory 会在 AstrBot `WakingCheckStage` 中先于所有消息 Handler 采集群消息，**`stop_event()` 无法阻止**。如需降低残留，请在其配置中考虑 `session_manager.enable_full_group_capture=false` 和/或 `access_control.whitelist_enabled + allowed_ids`（详见 COMPATIBILITY.md §6）。

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
OWNER；QQ 侧 Owner/Reviewer 列表**不**赋予 Dashboard 权限；无 `bot_self_id` 或伪造身份的管理操作返回 400 且不写库。

---

## ⚙️ 配置

配置项说明见 [docs/CONFIGURATION.md](docs/CONFIGURATION.md)。要点：

- 仅使用 `_conf_schema.json` 中真实生效的配置项；已移除无法兑现的 `application.duplicate_pending_allowed`、
  `security.fail_closed`、`security.gate_priority` 暴露项。
- `security.keep_audit_days` 在**插件启动时**清理过期审计日志（不是后台周期任务）。
- 申请人审批结果默认**仅私聊**发送；群内兜底默认关闭，开启后只发送不含审批理由的最小文案。

---

## 🛡️ 与第三方插件的兼容协同机制

### 1. GroupGuardian（全群内容审核）— 需要补丁

- **真实机制**：AstrBot 按 `handler.extras_configs["priority"]` **降序**执行；上游 GroupGuardian 主审核
  Handler 没有声明优先级（默认 0）。
- **支持部署**：应用 `patches/group_guardian_priority.patch`，把该 Handler 提升到 `priority=100`
  → 审核先于本插件闸门（50）→ 审核完成后再准入拦截。
- **插件行为**：只读真实注册表并报告 `VERIFIED` / `CONFLICT` / `NOT_INSTALLED` / `UNVERIFIED`；
  **绝不**在运行时修改其他插件的 Handler。
- **未打补丁**：属于不安全部署，插件会记录明确告警，**不保证**未授权用户消息被审核。

### 2. LivingMemory（长期记忆）— 无法在事件层阻止

- LivingMemory 通过自定义 filter 在 `WakingCheckStage` 调度 detached 采集任务，早于任何消息 Handler，
  且**不检查** `is_stopped()`。因此 `stop_event()` 不能阻止其存储未授权用户消息。
- 已存储消息在后续 LLM 回合（或管理员 `/lmem summarize`）可能被汇总进长期记忆。
- 本插件不提供、也不声称提供“记忆隔离”；缓解手段仅在 LivingMemory 配置侧（见上方“LivingMemory”小节）。

### 3. Jrys / Rollpig / Cyber Divination / AstrBot Agent

- 无论 `/前缀` 指令、无前缀短语（`今日运势`/`今日小猪`/`塔罗`）还是 `@Bot`，未获批成员都会在
  `priority=50` 闸门被统一阻断，无法触达这些插件的 Handler 或默认 LLM 管道。

---

## 🧪 自动化测试与质量保障

> **CI 状态：工作流已配置，但尚未在 GitHub 上实际运行；“CI 已通过”只能在 GitHub 跑过之后声明。**

本地实际结果（2026-11-09，本机 macOS / Python 3.13.17）：

| 命令 | 结果 |
|---|---|
| `.venv/bin/python -m pytest -q -p no:cacheprovider` | **117 passed, 1 skipped**（skip = 无 AstrBot 的单元测试环境中跳过真实框架测试） |
| `.venv/bin/ruff check .` | **All checks passed** |
| `.venv-astrbot/bin/python -m pytest tests/test_framework_handler_order.py -q` | **9 passed**（真实 AstrBot 4.29.4 注册表/事件） |

测试分级（详见 [INTEGRATION_TEST_REPORT.md](INTEGRATION_TEST_REPORT.md)）：

- **单元/服务级**：真实 SQLite，NapCat 与事件对象为假；
- **真实框架级**：`tests/test_framework_handler_order.py`，仅在 `.venv-astrbot`（AstrBot 4.29.4）运行，
  验证真实 `star_handlers_registry`、`extras_configs["priority"]`、真实 `AstrMessageEvent.stop_event()`；
- **真实 QQ/NapCat/Windows**：**未实测**。

缺陷与修复证据（含“旧代码失败 / 新代码通过”）见 [docs/DEFECT_MATRIX.md](docs/DEFECT_MATRIX.md)。

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
