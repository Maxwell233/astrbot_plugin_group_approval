# AstrBot Group Approval (群等级准入与管理员审批)

[![Tests](https://github.com/Maxwell233/astrbot_plugin_group_approval/actions/workflows/tests.yml/badge.svg)](https://github.com/Maxwell233/astrbot_plugin_group_approval/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python: 3.12+](https://img.shields.io/badge/Python-3.12+-green.svg)](https://www.python.org/)
[![AstrBot: v4.16+](https://img.shields.io/badge/AstrBot-v4.16+-orange.svg)](https://github.com/AstrBotDevs/AstrBot)

专门为 **AstrBot** 设计的高可靠性 QQ 群权限与安全中间件插件。基于 **NapCat OneBot v11** 获取的群成员真实等级（`memberRealLevel`），实现严格的 **“群等级达标 ➔ 提交使用申请 ➔ 管理员审批 ➔ 获得 Bot 使用权 ➔ 持续复核与自动冻结”** 完整授权生命周期。

> [!IMPORTANT]
> **版本声明：当前版本为 `0.1.1`（早期开发与调试阶段）**  
> 本项目目前处于**早期开发与调试阶段**，尚未达到生产环境的全面稳定运行标准，**不发布生产稳定版 Release**。所有未在真实 Windows + NapCat + QQ 群中实机验收的功能已标记为「待实测」，代码主要供开发者集成测试与联调。

---

## 🌟 核心特性

- 🎯 **真实群等级准入**：严格提取 NapCat `level`（群活跃等级），绝不与 QQ 账号等级（`qq_level`）混淆；支持强制刷新与单飞防抖。
- 🛡️ **Fail-Closed 故障阻断**：网络异常、退群、等级缺失或解析错误时，系统默认拒绝普通业务调用，杜绝意外放行。
- ⚖️ **角色与权限隔离**：
  - **Owner**：最高管理员，负责全局群策略、审核员增删、黑名单与手动授权。
  - **Reviewer**：审核员，负责日常审批、驳回与撤销。
  - **Approved User**：获批群成员，普通业务可用，绝不继承系统管理特权。
  - **QQ 群管隔离**：QQ 群主与群管理员不自动继承 Bot 管理员特权。
- 🔒 **全群内容审核兼容 (GroupGuardian)**：准入网关运行于 `priority = 50`，严格让行于安全审核插件（`priority = 100`），保证全群违规言论无差别审查。
- 🧠 **长期记忆隔离 (LivingMemory)**：在入口处截断未授权消息，阻断 LLM 管道，防止未获批用户的发言被写入个人长期记忆画像。
- 💾 **SQLite 事务与持久化**：采用 `aiosqlite` WAL 模式，天然具备并发原子性，两名管理员同时审批同一申请时只能有一人成功，不产生脏数据。
- 🔔 **持久化发信队列表**：审批通知写入 `notification_outbox` 表，支持重试与指数退避，网络瞬断不丢单。
- 🖥️ **双重管理方式**：同时提供丰富的 QQ 指令集与现代响应式 **AstrBot 原生 WebUI 仪表盘**。

---

## 📋 运行环境要求

- **操作系统**：Windows 10 / 11 64位（亦兼容 macOS / Linux）
- **Python 环境**：Python 3.12+
- **协议端**：[NapCatQQ](https://github.com/NapNeko/NapCatQQ)（OneBot v11 反向 WebSocket）
- **主框架**：[AstrBot](https://github.com/AstrBotDevs/AstrBot) v4.16.0+（由 AstrBot Launcher 启动）
- **数据库**：内置 SQLite 3（WAL 模式，无需单独配置外部数据库）

---

## 🚀 安装部署方法

### 方式一：AstrBot Launcher / WebUI 扩展商店（推荐）

1. 登录 AstrBot 管理控制台。
2. 导航至 **扩展 -> 插件**，点击右上角 **从 GitHub 仓库安装**。
3. 输入仓库地址：
   ```text
   https://github.com/Maxwell233/astrbot_plugin_group_approval
   ```
4. 点击 **安装**，系统将自动拉取最新源码并安装依赖。
5. 在插件配置中指定你的 **Owner QQ 号**，随后重启或热重载 AstrBot。

### 方式二：手动克隆至插件目录

1. 在终端中切换到 AstrBot 的插件目录：
   ```powershell
   cd <AstrBot安装目录>\data\plugins
   git clone https://github.com/Maxwell233/astrbot_plugin_group_approval.git astrbot_plugin_group_approval
   ```
2. 在 AstrBot 的 Python 环境中安装依赖：
   ```powershell
   pip install -r astrbot_plugin_group_approval\requirements.txt
   ```
3. 重启 AstrBot 实例。

---

## 📖 使用与审批全流程

```text
群成员在 QQ 群发送「/申请使用」
             ↓
检查目标群是否开启准入 / 黑名单 / 已有授权
             ↓
调用 NapCat 穿透拉取真实群等级
             ↓
【未达门槛】：直接返回当前等级与门槛要求，不入库
【满足门槛】：入库生成 PENDING 申请单，发送私聊提醒给管理员
             ↓
管理员私聊或在群内发送「/ga approve <编号>」或登录 WebUI 点击“批准”
             ↓
SQLite 事务原子更新申请单为 APPROVED，创建 ACTIVE 授权
             ↓
系统向用户私聊发送通过通知，用户获得该群的 Bot 使用权限
```

---

## ⌨️ QQ 指令清单

### 用户准入指令（无需预先授权即可使用）

| 指令 | 描述 | 使用限制 |
|---|---|---|
| `/申请使用 [理由]` | 提交当前群的 Bot 使用资格申请 | 仅限群聊 |
| `/我的权限` | 查看自己在当前群的授权状态、到期时间与最后核验等级 | 仅限群聊 |
| `/申请状态` | 查询自己正在等待审批的申请进度 | 仅限群聊 |
| `/取消申请` | 主动取消自己提交的待审核申请 | 仅限群聊 |
| `/使用规则` | 查看当前群的最低申请门槛、审批要求与有效期 | 仅限群聊 |

### 管理员指令 (`/ga ...`)

#### Reviewer（审核员）可用：
- `/ga pending [群号]` - 查看待审核申请列表
- `/ga approve <申请编号> [备注]` - 批准指定申请
- `/ga reject <申请编号> [原因]` - 拒绝指定申请并进入冷却期
- `/ga revoke <群号> <QQ> [原因]` - 撤销某用户的活跃授权
- `/ga user <群号> <QQ>` - 查看目标用户的授权档案与最近申请
- `/ga list <群号>` - 查看该群获批用户列表
- `/ga history <申请编号>` - 查看申请处理历史详情

#### Owner（最高管理员）专属：
- `/ga grant <群号> <QQ> [天数]` - 手动授予用户使用权限（默认0为永久）
- `/ga ban <群号> <QQ> [原因] [天数]` - 将用户加入黑名单并自动撤销授权
- `/ga unban <群号> <QQ>` - 将用户从黑名单移出
- `/ga reviewer add <QQ>` - 添加指定 QQ 为审核员
- `/ga reviewer remove <QQ>` - 移除指定 QQ 的审核员身份
- `/ga level <群号> <数字>` - 调整群最低申请门槛等级
- `/ga enable <群号>` - 启用群准入鉴权
- `/ga disable <群号>` - 禁用群准入鉴权
- `/ga status` - 查看系统运行概况与统计数据

> **提示**：Owner 默认可在私聊中向机器人发送所有 `/ga` 管理指令。

---

## 🖥️ WebUI 管理看板

本插件基于 AstrBot 原生 Plugin Pages 体系实现了自适应现代管理面板：

1. **运行概览 (Dashboard)**：显示受管群数、待审申请数、活跃成员数、被暂停用户数及最新审批动态。
2. **申请审批 (Applications)**：支持群号/QQ号多条件检索，一键批准或拒绝申请，支持自定义审批备注。
3. **用户与授权 (Users & Grants)**：查看全局授权档案，支持手动添加授权、撤销授权以及拉黑。
4. **群组策略 (Groups)**：动态配置每个群的最低等级、审批模式、有效期、复核间隔与恢复策略。
5. **审计日志 (Audit Logs)**：全量记录所有审批、撤销、提权动作，支持导出 **JSON** 与 **CSV**。

---

## ⚙️ 默认与每群独立策略

可直接在 WebUI 或编辑配置自定义：

```yaml
groups:
  "123456789":
    enabled: true                     # 开启群准入
    min_level: 10                     # 最低申请群等级 LV10
    approval_required: true           # 达到等级后必须提交申请并由管理员批准
    grant_duration_days: 0            # 授权有效期（0为永久）
    recheck_interval_seconds: 300     # 300 秒复核一次等级
    restore_policy: "automatic"       # 等级跌落后恢复自动激活（或 manual 手动审批）
    owner_bypass: true                # 插件 Owner 豁免准入
    group_admin_bypass: false         # QQ群管理员是否自动豁免门槛
```

---

## 🛡️ 与第三方插件的兼容协同机制

### 1. GroupGuardian（全群内容审核）
- **时序保证**：`GroupGuardian` 运行在 `priority = 100`，`Group Approval` 运行在 `priority = 50`。
- **协同效果**：未获批成员发送的所有消息（包括违规文字与图片）均最先被 GroupGuardian 审核、撤回或禁言。随后 Group Approval 才会阻止未违规的普通消息调用 Bot。审核能力完全不受影响。

### 2. LivingMemory（智能长期记忆）
- **隔离保障**：Group Approval 对未获批用户的发言调用 `event.stop_event()`，彻底终止事件进入 LLM 管道。下游的 `@filter.on_llm_request` 钩子不会触发，绝不采集未授权用户的言论生成个人画像。

### 3. Jrys / Rollpig / Cyber Divination / AstrBot Agent
- **拦截保障**：无论是一般带有 `/` 前缀的指令，还是 `今日运势`、`今日小猪`、`塔罗` 等无前缀短语或 `@Bot`，未获批成员均被集中闸门阻断，无法绕过。

---

## 🧪 自动化测试与质量保障

项目配置了完整的自动化测试套件与 GitHub Actions CI 流水线：

```powershell
pytest -v
```

**测试覆盖项（34/34 全部通过）：**
- 群等级各种字符串/整数格式解析与正则提取（`LV18`, `18`, `等级20`）
- 群等级 `level` 与账号等级 `qq_level` 严格隔离测试
- 300秒 TTL 缓存命中与 force_refresh 穿透测试
- 并发单飞请求合并防抖测试
- 申请达标生成、不足拒绝、冷却期拦截与防重复申请测试
- SQLite 事务并发审批原子性（两人并发审批只有 1 人成功，无脏数据）
- 授权撤销、黑名单优先、跨群隔离、跨 Bot QQ 号隔离测试
- 授权成员等级掉出要求自动挂起为 SUSPENDED，回升后自动恢复测试
- 拦截闸门时序、GroupGuardian 审核先行与 LivingMemory 记忆隔离测试
- WebUI 仪表盘、申请/群组更新 API 及 CSV/JSON 审计导出测试

---

## 💾 数据备份建议

插件的数据库文件独立保存在 AstrBot 持久化数据目录：
```text
<AstrBot根目录>/data/astrbot_plugin_group_approval/group_approval.db
```
- 该文件在插件代码更新或 Git 拉取时**绝不会被覆盖**。
- 支持 SQLite 在线热备份（通过标准 SQLite 复制或 AstrBot 备份工具备份）。

---

## 📝 常见问题与排查

详细问题排查请参阅 [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)。

---

## 📄 开源许可证

本项目采用 [MIT 许可证](LICENSE) 开源。欢迎提交 Pull Request 与 Issue。
