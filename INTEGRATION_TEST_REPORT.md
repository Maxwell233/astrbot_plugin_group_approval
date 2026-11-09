# 集成测试与验证报告 (INTEGRATION_TEST_REPORT.md)

**当前版本：** `0.1.1`（开发调试版，非生产稳定版）
**报告时间：** 2026-11-09 19:37（Europe/Berlin）
**被测提交：** `ec016a2`（`fix/0.1.1-security-integration`，在 `eba75d8` 之上 5 个修复提交）
**测试环境：** macOS（Apple Silicon）/ Python 3.13.17 / pytest 9.2.0 / pytest-asyncio 1.4.1.post1 / ruff 0.17.4 / aiosqlite 0.22.1
**真实框架环境：** `.venv-astrbot`，PyPI **AstrBot 4.29.4**（独立于单元测试 `.venv`）

> [!WARNING]
> **版本与验证范围说明**：本项目仍处于早期开发与调试阶段，**不发布正式稳定版 Release**。
> 本报告严格区分四类测试与真实环境验收（见第 1 节）。**真实 Windows + NapCat + QQ 群联调未执行**；
> GitHub Actions 工作流已配置，但**尚未在 GitHub 实际运行**，因此在它真正跑通前不得声称“CI 已通过”。

---

## 1. 测试分级（Test Taxonomy）——真实 vs 模拟

| 级别 | 定义 | 运行环境 | 真实组件 | 仍为模拟/替代的组件 |
|---|---|---|---|---|
| **(a) 单元/服务级** | 直接调用服务类函数，断言业务规则 | `.venv`（无 AstrBot） | 真实 Python 服务代码；多数使用真实 SQLite | NapCat 传输层、AstrBot 事件对象、框架装饰器 |
| **(b) 服务级集成（真实 SQLite）** | 真实 `Database`/迁移/事务 + 真实服务栈 | `.venv` | **真实 SQLite（aiosqlite）**、真实迁移 SQL、真实事务/并发 | NapCat 客户端与 `AstrMessageEvent` 为假；AstrBot 框架不存在 |
| **(c) 真实 AstrBot 框架级** | 安装真实 AstrBot 4.29.4，导入真实框架对象 | **`.venv-astrbot`**（AstrBot 4.29.4） | 真实 `filter` 装饰器、真实全局 `star_handlers_registry`、真实 `extras_configs["priority"]`、真实 `AstrMessageEvent` 与 `stop_event()/is_stopped()`、真实插件 `on_message_gate` 注册 | 仅“分发循环”按上游源码复刻（见第 4 节）；不启动 AstrBot 主进程/StarManager |
| **(d) 真实 QQ / NapCat / Windows** | 真实 NapCat + 真实 QQ 群 + Windows 生产机 | 无 | — | **完全未执行（未实测）** |

### 1.1 文件清单与计数（本次实际收集）

| 级别 | 文件 | 收集数 |
|---|---|---|
| a/b | `tests/test_access.py` | 7 |
| a/b | `tests/test_applications.py` | 6 |
| a/b | `tests/test_bot_identity.py` | 22 |
| a/b | `tests/test_commands.py` | 2 |
| a/b | `tests/test_concurrency.py` | 2 |
| b | `tests/test_db_transaction_isolation.py` | 6 |
| b | `tests/test_grant_state_race.py` | 4 |
| a | `tests/test_group_guardian_patch.py` | 3 |
| b | `tests/test_integration.py` | 6 |
| a | `tests/test_level_service.py` | 9 |
| b | `tests/test_migrations.py` | 3 |
| b | `tests/test_notification_privacy.py` | 6 |
| a/b | `tests/test_v011_features.py` | 9 |
| a/b | `tests/test_v011_round2.py` | 6 |
| a/b | `tests/test_web_api.py` | 3 |
| b | `tests/test_web_api_params.py` | 23 |
| c | `tests/test_framework_handler_order.py` | 9（`.venv` 中整体跳过） |

合计：`.venv` 收集 **117 passed + 1 skipped**；`.venv-astrbot` 中框架文件单独运行 9 passed。

### 1.2 关键“真实 vs 模拟”说明

- **真实**：所有 SQLite 数据库（文件级、WAL、迁移 V1–V6、事务隔离、并发）、服务层业务规则、
  阈值/状态机、AstrBot 4.29.4 的注册表与事件对象（c 级）。
- **模拟**：NapCat OneBot 传输（`MockNapCatClient` / `_FakeNapCat` / `AsyncMock`）、AstrBot 事件对象
  （a/b 级中的 `DummyEvent`/`MockAstrEvent`/`MagicMock`）、`AstrBotPluginView` 前端桥（无浏览器）。
- **不存在**：真实 QQ 号、真实群、真实 NapCat 进程、真实 GroupGuardian/LivingMemory 插件运行。

---

## 2. 本次实际执行结果（可复现）

执行时间 2026-11-09 19:36-19:37（Europe/Berlin），工作树已包含全部修复提交 `ec016a2`。

### 2.1 单元/服务级（`aiosqlite` + pytest，无 AstrBot）

```bash
cd <repo>
.venv/bin/python -m pytest -q -p no:cacheprovider
```

逐字结果：

```text
117 passed, 1 skipped in 0.62s
```

- **通过 117 / 失败 0 / 错误 0 / 跳过 1**（exit code 0）。
- 唯一 skip 原因（`-rs` 输出）：
  `SKIPPED [1] tests/test_framework_handler_order.py:26: could not import 'astrbot': No module named 'astrbot'`
  ——即 c 级真实框架测试在无 AstrBot 的 `.venv` 中整体跳过，属于设计内行为。
- 本次运行**无 warning**；历史上偶发 `PytestUnhandledThreadExceptionWarning`（`aiosqlite` 线程在测试收尾
  时向已关闭事件循环回投结果）。该 warning **不会使测试失败**，属于测试收尾竞态，非功能/安全缺陷。

### 2.2 静态检查

```bash
.venv/bin/ruff check .
```

逐字结果：

```text
All checks passed!
```

### 2.3 真实 AstrBot 框架级（c 级，`AstrBot 4.29.4`）

```bash
.venv-astrbot/bin/python -m pytest tests/test_framework_handler_order.py -q -p no:cacheprovider
```

逐字结果：

```text
9 passed in 1.49s
```

### 2.4 前端语法检查

```bash
node --check views/dashboard/app.js
# -> app.js OK（exit 0）
```

仅语法级检查；**真实浏览器/Dashboard iframe 行为未实测**。

---

## 3. 环境验证状态表（已实测 / 未实测 / 无法验证）

| 环境项 | 状态 | 证据 / 说明 |
|---|---|---|
| 单元/服务级（含真实 SQLite） | **已实测** | 117 passed（本次运行，提交 `ec016a2`） |
| AstrBot 4.29.4 真实导入、真实 Handler 注册表、真实 `AstrMessageEvent`/`stop_event()` | **已实测** | `.venv-astrbot`（PyPI AstrBot 4.29.4）；`tests/test_framework_handler_order.py` 9 passed |
| AstrBot StarManager 真实加载/热重载/卸载插件 | **未实测** | 未启动 AstrBot 主进程；只在测试中导入/实例化/`initialize()/terminate()` |
| 真实 NapCatQQ 连接 | **未实测** | 全部使用假传输层；无真实 OneBot 连接 |
| 真实 QQ 测试群 | **未实测** | 无真实 QQ 号/群 |
| Windows 10/11 + AstrBot Launcher 生产部署 | **未实测** | 本机为 macOS |
| GroupGuardian 真实联动（安装 + 补丁 + 真实消息） | **未实测** | 补丁内容有契约测试；真实注册表顺序用代理 Handler 验证（c 级）；未运行真实 GroupGuardian |
| LivingMemory 真实联动/画像 | **未实测 / 无法验证** | 仅上游 LivingMemory 2.10.0 源码静态核对；`stop_event()` 无法阻止其采集 |
| WebUI 真实 Dashboard iframe 桥接 | **未实测** | 前端仅 `node --check` 语法检查 |
| GitHub Actions CI | **未实测** | 工作流已配置；未在 GitHub 运行，**不得声称 CI 已通过** |

---

## 4. 真实 AstrBot 框架级测试（c 级）说明

文件：`tests/test_framework_handler_order.py`（仅 `.venv-astrbot` 运行；`.venv` 中整体 skip）。

**真实的部分：**

- 真实 `@filter.event_message_type(..., priority=...)` 装饰器与真实注册表
  `astrbot.core.star.star_handler.star_handlers_registry`；
- 真实 `handler.extras_configs["priority"]`（降序：100 → 50 → 0）与真实 `get_handlers_by_event_type` 顺序；
- 真实 `AstrMessageEvent` 实例与真实 `stop_event()/is_stopped()`；
- 真实插件 `main.GroupApprovalPlugin` 导入后注册的 `on_message_gate`（真实 `priority=50`）；
- 真实插件在 `initialize()/terminate()` 下的 gate 端到端行为（未授权拦截、准入指令本地执行并 stop、
  已授权放行、他人仍被拦）。

**仍为模拟的部分（已在文件头声明）：** 分发循环本身。测试按上游源码
`astrbot/core/pipeline/process_stage/method/star_request.py:39-41,88-89` 复刻“调用前/后检查
`is_stopped()` 即 break”的循环；这不是 AstrBot 主流水线的完整运行。

---

## 5. 修复回归证据（旧代码失败 / 新代码通过）

来自 [docs/DEFECT_MATRIX.md](DEFECT_MATRIX.md)（详细矩阵见该文件）：

| 缺陷 | 旧代码复现 | 新代码 |
|---|---|---|
| BUG-001 GroupGuardian 审核被闸门抢占 | 真实注册表：gate(50) 先于 stock GG(0)，GG 完全不运行 | 补丁 GG(100)→gate(50)→business(0)，业务永不运行（c 级测试） |
| BUG-002 事务协程隔离 | `tests/test_db_transaction_isolation.py`：3 项失败 + 嵌套事务死锁 | 6 passed |
| BUG-004 授权状态竞态 | `tests/test_grant_state_race.py`：4 项全部失败（REVOKED 被改成 SUSPENDED 等） | 4 passed |
| BUG-011 Reviewer 持久化 | 删除后重启从旧配置复现 | V5 `reviewer_overrides` + 回归测试 |
| BUG-013 迁移升级 | 旧库重复 `event_key` 导致 V2 唯一索引失败 | V2 先去重再建索引；V1–V3 数据保留（`tests/test_migrations.py`） |
| BUG-014 版本下限 | 声明 `>=4.16.0` 与代码不符 | `>=4.26.0`（`metadata.yaml`） |

> 这些是**代码级/框架注册表级**回归证据，不等于真实 QQ 环境验收。

---

## 6. 真实环境待实测清单（Pending Live Verification）

> [!NOTE]
> 下列项目**未**在真实物理环境验收；使用前请在测试群逐步确认：

1. 真实 Windows 10/11 生产机的 AstrBot Launcher 常驻、插件热重载。
2. 真实 NapCatQQ 二进制读取 `get_group_member_info.level` 的延迟、风控与 `no_cache` 行为。
3. 真实 QQ 私聊风控对审批通知到达率的影响。
4. 真实 GroupGuardian 安装 + 应用 `patches/group_guardian_priority.patch` 后的实际执行顺序与审核效果。
5. 真实 LivingMemory 运行时的消息采集与长期记忆生成（本插件不宣称能阻止，仅记录限制）。
6. 真实 WebUI Dashboard 中 Plugin View Bridge 的调用与导出下载。
7. GitHub Actions 两个 job 的实际运行结果（在 GitHub 跑过之前不得声明“CI 已通过”）。

---

## 7. 复现命令（本地）

```bash
# 单元/服务级 + 静态检查（无 AstrBot）
.venv/bin/python -m pytest -q -p no:cacheprovider
.venv/bin/ruff check .

# 真实 AstrBot 4.29.4 框架级（框架测试会自动跳过 a/b 级之外的环境）
.venv-astrbot/bin/python -m pytest tests/test_framework_handler_order.py -q -p no:cacheprovider

# 可选：前端语法
node --check views/dashboard/app.js
```

环境搭建：`.venv` 使用 `python3 -m venv .venv && .venv/bin/pip install "aiosqlite>=0.20.0" pytest pytest-asyncio ruff`；
`.venv-astrbot` 使用 `python3 -m venv .venv-astrbot && .venv-astrbot/bin/pip install "AstrBot==4.29.4" pytest pytest-asyncio`。
