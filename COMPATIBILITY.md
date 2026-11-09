# AstrBot 插件兼容性与架构调研报告 (COMPATIBILITY.md)

**更新时间：** 2026-11-09（基于修复后的当前工作树；本文不描述已废弃的旧实现）
**插件：** `astrbot_plugin_group_approval` v0.1.1（**开发调试版**，非稳定版 Release）
**目标运行时：** **AstrBot >= 4.26.0** + NapCat OneBot v11 + Python 3.12+
**支持平台：** 仅 `aiocqhttp`（见 `metadata.yaml`）

**依据（可复核）：**

| 证据源 | 说明 |
|---|---|
| `upstream/AstrBot`（tag **v4.29.4**，commit `6133541`） | AstrBot 框架源码只读核对（优先级、`stop_event`、注册表、Plugin Web API） |
| `.venv-astrbot`（PyPI **AstrBot 4.29.4** 实际安装） | **真实框架**导入与注册表探测，由 `tests/test_framework_handler_order.py` 覆盖（本机实测 9 passed） |
| `tests/test_framework_handler_order.py` | 真实 AstrBot 4.29.4 下的注册表/优先级/事件对象测试（9 项，见集成测试报告） |
| `upstream/group_guardian`（v2.8.11，`569f6bb`） | 上游 GroupGuardian 源码核对（无 `priority=`，默认 0） |
| `upstream/livingmemory`（2.10.0，`1a216d7`） | 上游 LivingMemory 源码核对（`WakingCheckStage` 旁路采集） |

> 本文所有“真实框架行为”结论都来自源码 + 本机 AstrBot 4.29.4 的真实导入/注册表实验；
> 真实 QQ/NapCat/Windows 环境行为**未实测**，见第 9、10 节。

---

## 1. 版本兼容矩阵

| 组件 | 已验证/参照版本 | 兼容要求 | 证据 |
|---|---|---|---|
| **AstrBot** | **4.29.4**（PyPI 安装于 `.venv-astrbot`；源码 tag v4.29.4） | **>= 4.26.0** | `astrbot/api/web.py`（`request`/`json_response`）在 4.26.0 之前不存在；`stop_event()` 在 4.25.0 之前不会终止后续插件 Handler。二者都被本插件直接使用，故最低 4.26.0。`metadata.yaml` 已声明该下限 |
| **NapCatQQ** | OneBot v11 反向 WebSocket 协议 | 提供 `get_group_member_info` / `send_private_msg` / `send_group_msg` / `get_login_info` | 代码调用点 `approval/napcat_client.py`；真实 NapCat **未实测** |
| **Python** | 3.13.17（本机）；CI 目标 3.12 / 3.13 | 3.12+ | AstrBot 4.29.4 要求 `Requires-Python >=3.12` |
| **GroupGuardian** | 上游 v2.8.11（MIT） | **必须应用 `patches/group_guardian_priority.patch`** 后才保证“审核先于准入” | 见第 5 节；未打补丁时为 CONFLICT，**不保证**审核未授权用户消息 |
| **LivingMemory** | 上游 2.10.0（AGPL-3.0） | 本插件**无法**在事件层阻止其采集；需按其配置项缓解 | 见第 6 节 |

**结论：** 旧文档中的 `AstrBot >= 4.16.0` 是错误声明，已删除；`>= 4.26.0` 是代码路径决定的硬下限，
不是保守估计。

---

## 2. AstrBot Star 插件生命周期与数据目录

- **插件基类**：`astrbot.api.star.Star`；AstrBot 通过 `Star` 子类 + `metadata.yaml` 自动发现，无需 `@register`。
- **初始化钩子**：AstrBot 会 **await** `async def initialize(self)` 后再认为插件加载完成。本插件把
  数据库建表/迁移、过期清理、审计保留清理、审核员同步、Web 路由注册、通知 worker 启动都放在
  `initialize()`（`main.py:281-304`），不使用 `__init__` 里的 fire-and-forget task，加载失败可见。
- **卸载钩子**：`async def terminate(self)` 取消通知 worker 并关闭 SQLite（`main.py:324-333`）。
- **数据目录**：`StarTools.get_data_dir()` 返回 **`data/plugin_data/{plugin_name}`**（AstrBot
  `astrbot/core/star/star_tools.py:244-257`）。本插件数据库文件位于该目录下的
  `group_approval.db`；实际路径以运行环境日志/目录为准（插件在独立测试环境会退化为 `data/...`）。
- **Web 路由注册时机**：`initialize()` 内调用 `context.register_web_api(...)`；若宿主是旧版 AstrBot
  （无该 API）则跳过并记录 warning（`main.py:337-371`），不会阻断插件加载。

---

## 3. Handler 优先级机制（真实 AstrBot 4.29.4）

**事实（源码 + 实测一致）：**

1. 优先级存放在 **`handler.extras_configs["priority"]`**；默认 `0`。
   - `astrbot/core/star/star_handler.py:19-22`（append 时补 0）。
2. 注册表以 **数值降序** 排序：`self._handlers.sort(key=lambda h: -h.extras_configs["priority"])`
   （`star_handler.py:26`）。**数值越大越先执行。**
3. 相同优先级没有显式次级排序键：Python `list.sort` 稳定，因此 **按注册/加载顺序** 保持原有先后。
4. 真实注册表路径为 `astrbot.core.star.star_handler.star_handlers_registry`
   （**不存在** 旧资料里的 `astrbot.core.star.star_handlers_registry` 模块）。
5. `get_handlers_by_event_type(..., only_activated=False)` 按上述真实顺序返回 Handler 列表。

**本插件的布局：**

| Handler | 优先级 | 位置 |
|---|---|---|
| GroupGuardian 主审核（**打补丁后**） | **100** | 上游 `group_guardian/main.py` 装饰器（补丁） |
| Group Approval 准入闸门 `on_message_gate` | **50**（`GATE_PRIORITY`） | `main.py:493`、`approval/gate.py:36` |
| 普通业务插件 / AstrBot 默认插件 | 0（或负数） | 第三方 |

**证据：** 真实注册表测试 `tests/test_framework_handler_order.py:139-200` 断言
`extras_configs["priority"]` 实际值、降序顺序、以及“补丁 100 → 闸门 50 → 业务 0”的 dispatch 顺序；
该顺序由真实 AstrBot 4.29.4 测试 `tests/test_framework_handler_order.py` 断言（本机实测 9 passed）。

> 注意：接入真实框架测试时，若只 import 插件模块而未经过 StarManager 加载，默认
> `get_handlers_by_event_type(only_activated=True)` 不返回未激活 Star 的 Handler；
> 集成测试使用 `only_activated=False`。

---

## 4. `event.stop_event()` 的真实边界

**定义（真实 AstrBot 4.29.4）：** `astrbot/core/platform/astr_message_event.py:418-440`

```python
def stop_event(self) -> None:
    self._force_stopped = True
    if self._result is None:
        self.set_result(MessageEventResult().stop_event())
    else:
        self._result.stop_event()

def is_stopped(self) -> bool:
    if self._force_stopped:
        return True
    if self._result is None:
        return False
    return self._result.is_stopped()
```

- `_force_stopped` 是**粘性标志**，`clear_result()` 不会清除它；只有 `continue_event()` 才会重置。
- **能阻止什么**：AstrBot 4.25+ 的插件 Handler 分发循环在调用下一个 Handler **之前和之后**都检查
  `is_stopped()` 并 `break`（`astrbot/core/pipeline/process_stage/method/star_request.py:39-41,88-89`），
  所以会阻止**所有后续（含同优先级）插件 Handler**，并抑制默认 LLM 请求。
- **不能阻止什么**：
  - 不能撤销已经执行过的更高优先级 Handler（例如补丁后的 GroupGuardian 100）。
  - 不能阻止 AstrBot 继续走后续 pipeline 阶段：已经产生的回复仍会被装饰/发送
    （`pipeline/respond/stage.py` 不检查 `is_stopped()`）。
  - **不能阻止 LivingMemory 在 `WakingCheckStage` 中已经调度出去的旁路采集任务**（见第 6 节）。
- 版本差异：4.16–4.24.x 的分发循环没有 `is_stopped()` 检查，`stop_event()` 不会阻止后续插件 Handler；
  这是最低版本必须 **>= 4.26.0** 的原因之一。

---

## 5. GroupGuardian 兼容性：补丁是**必须的部署步骤**

**上游现状（`upstream/group_guardian` v2.8.11，`569f6bb`）：** 所有 Handler 都没有显式 `priority=`，
即默认 **0**。主审核入口为 `_handle_message`（`main.py:644-648` → `moderation.py:2003`），
内部所有审核动作（含 LLM 调用、撤回/禁言）都是 **inline await**，没有独立的审核后台任务。

**因此，未打补丁时：**

```text
未授权用户违规消息
  → 本插件闸门 priority=50 先执行 → stop_event()
  → GroupGuardian 审核 priority=0 根本不会运行
  → 未授权用户的违规内容不会被审核
```

这是 v0.1.1 的 **P0 缺陷（BUG-001）**。AstrBot 没有“审核槽位”，也不支持一个插件在运行时安全地修改
另一个插件的 Handler 状态，所以本插件**绝不**做运行时 monkey-patch，而是要求部署方给 GroupGuardian
应用补丁。

**受支持部署（必须执行）：**

```bash
# 在 GroupGuardian 插件目录内（先备份 main.py）
git apply --check /path/to/astrbot_plugin_group_approval/patches/group_guardian_priority.patch
git apply         /path/to/astrbot_plugin_group_approval/patches/group_guardian_priority.patch
```

`patches/group_guardian_priority.patch` **只改一行装饰器**：把 `_handle_message` 的
`@filter.event_message_type(filter.EventMessageType.ALL)` 改为
`@filter.event_message_type(filter.EventMessageType.ALL, priority=100)`；不动其它 Handler。
GroupGuardian 为 **MIT** 许可（Copyright (c) 2025 zhaisir），补丁保留其版权声明，说明见
`patches/README.md`。

**运行期只做“读取真实注册表并报告”，不修改任何 Handler：**

- `HandlerDiagnostics` 导入 `star_handlers_registry`，读取每个 Handler 的真实
  `handler_module_path` / `handler_name` / `extras_configs["priority"]`
  （`approval/diagnostics.py:31-144`），返回 `moderation_precedence` 四态：

| 状态 | 含义 |
|---|---|
| `VERIFIED` | 发现 GroupGuardian Handler，且其优先级 **>** 闸门优先级（50）→ 审核先于准入 |
| `CONFLICT` | 发现 GroupGuardian，但优先级 **<=** 50 → 闸门会抢先请求处理，**不保证**内容审核 |
| `NOT_INSTALLED` | 真实注册表中没有 GroupGuardian Handler |
| `UNVERIFIED` | 无法导入真实 AstrBot 注册表（框架过旧/导入失败）→ 不假设安全 |

- 启动时在日志中输出上述结论（`main.py:309-323`）；只读 HTTP 接口
  `GET /api/v1/plugins/extensions/astrbot_plugin_group_approval/diagnostics`（经 AstrBot 插件 Web 路由）返回同样数据。
- **不要**把 `VERIFIED` 理解为“已在真实 QQ 环境验证”；它仅表示**当前进程真实注册表**中的顺序符合预期。

---

## 6. LivingMemory：`stop_event()` 不能阻止其采集（重要更正）

**旧文档错误：**“未授权消息被 stop_event 拦截 ⇒ LLM 请求不发起 ⇒ 不会生成画像”。这是**错误**的。

**上游实际机制（LivingMemory 2.10.0）：**

1. 被动采集不是普通消息 Handler，而是注册为 **`@filter.custom_filter(PassiveGroupCaptureFilter, False)`**
   的过滤器（`livingmemory/main.py:451-456`）；`filter()` 在 AstrBot 的
   **`WakingCheckStage`**（任何消息 Handler 执行之前）被同步调用。
2. 该过滤器在返回 `False` 之前就**调度了一个 detached asyncio task** 去写会话库
   （`core/passive_group_capture.py:120-144`；`main.py:334-361`）。
3. 采集任务**不检查 `event.is_stopped()`**；`enable_full_group_capture` 默认 `true`，即所有群消息
   （含未 @Bot 的）都会被写入 `conversations.db`。
4. 所以：**本插件在 priority=50 调用 `stop_event()` 时，LivingMemory 的采集任务早已被调度**，
   未授权用户的发言仍会被存储。LLM 相关的 recall/reflection 钩子对“已停止”的事件不会运行，
   但这些已存储的消息**可以在之后同一会话的 LLM 回合（或管理员 `/lmem summarize`）里被汇总进长期记忆**。

**缓解（没有官方“禁止某用户/某群”的插件 API）：**

| 选项（LivingMemory 配置） | 默认 | 作用 |
|---|---|---|
| `session_manager.enable_full_group_capture` | `true` | 设为 `false` 可全局关闭群消息被动采集 |
| `access_control.whitelist_enabled` + `access_control.allowed_ids` | `false` / 空 | 仅允许清单内的身份进入采集/召回 |
| `profile_settings.enabled` | `true` | 关闭画像读写（**不**停止原始消息采集） |

**结论：** 本插件**无法**保证未授权用户的发言不被 LivingMemory 存储；只能通过上述配置在
LivingMemory 侧缓解，或接受该残留风险。**未在真实运行环境验证**（见第 10 节）。

---

## 7. AstrBot WebUI：插件页面桥、API 路由与身份模型

- **静态资源目录：** AstrBot 4.26+ **优先 `views/`，`pages/` 作为别名**（上游
  `dashboard/services/plugin_page_service.py:27-29`）。本仓库只保留 `views/dashboard/`，不要再维护两份。
- **前端桥：** `window.AstrBotPluginView`（别名 `window.AstrBotPluginPage`），调用
  `bridge.apiGet(path, params)` / `bridge.apiPost(path, payload)`。
  前端实现见 `views/dashboard/app.js:10-28,249-272`。
- **路由映射：** 桥/HTTP 最终访问
  `/api/v1/plugins/extensions/<plugin_name>/<endpoint>`（`views/dashboard/app.js:21`）。
  **旧 `/api/plug/...` 在 4.26+ 已不存在**，不要使用。
- **后端注册：** `context.register_web_api(f"/{PLUGIN_NAME}/...", handler, ["GET"|"POST"], description)`
  （`main.py:337-371`）。端点列表：`bots`、`dashboard`、`diagnostics`、`applications`、
  `applications/approve|reject`、`users`、`users/grant|revoke|ban|unban`、`groups`、`groups/update`、
  `audit_logs`、`audit_logs/export`。
- **身份模型（真实 AstrBot 4.29.4）：** AstrBot 只有**一个 Dashboard 账号**
  （`dashboard.username`）；插件 Handler 能拿到的唯一官方身份信号是 **`request.username`**
  （由 AstrBot 在 JWT/API key 鉴权后注入）。不存在 dashboard 角色表。
  - 只有 `request.username` **完全等于**配置的 `dashboard.username` 才视为 **OWNER**；
  - QQ 侧 Owner/Reviewer 名单（`owner_qq_ids` / `reviewer_qq_ids`）**不会**赋予 Dashboard 权限；
  - `api_key:` 身份可用于读取，但**不能**执行变更操作；
  - 未知/无法证明的身份一律拒绝变更操作（`approval/auth_service.py:50-161`）。
- **HTTP 状态码说明：** AstrBot 网关会先拒绝未认证请求；插件自身对“已认证但无权限”的响应是
  **HTTP 200 + body 中的 `retcode`（401/403）**，不是插件直接返回 HTTP 401/403。请据 `retcode` 判断。

---

## 8. Bot 身份与多 Bot 路由

- **平台实例的 `self_id` 不是 Bot QQ 号**（aiocqhttp 的 client/instance id 是 UUID 一类的连接标识）。
  Bot QQ 必须通过以下方式获得/验证：
  1. 入站事件的 **`event.get_self_id()`**（适配器从已认证连接写入）；和/或
  2. OneBot API **`get_login_info`** 的返回。
- 所有需要指定 Bot 的 OneBot 动作都显式传 `self_id=<已核验 QQ>` 进行路由
  （`approval/napcat_client.py:499-575`）。
- **未知 Bot 标识 fail closed**：没有“第一个客户端兜底”，业务路径不写 `"default"` 占位
  （`napcat_client.py:469-497`；`web_api.py` 对缺失身份返回错误）。
- 仪表盘 `/bots` 返回**已核验/已知**的 Bot 标识集合（优先来自 NapCatClient 的已核验缓存，
  否则来自数据库），并过滤 `"default"` 占位（`approval/web_api.py:380-398`）；它不是“当前在线实例列表”的承诺。

---

## 9. 环境验证状态表（已实测 / 未实测 / 无法验证）

| 环境项 | 状态 | 说明 |
|---|---|---|
| 单元/服务级测试（真实 SQLite + 假 NapCat/事件） | **已实测** | 见 `INTEGRATION_TEST_REPORT.md` 实际计数 |
| AstrBot 4.29.4 真实导入 / 真实 Handler 注册表 / 真实 `AstrMessageEvent` | **已实测** | `.venv-astrbot`（PyPI AstrBot 4.29.4）；`tests/test_framework_handler_order.py` 9 项通过 |
| AstrBot 真实 StarManager 加载/热重载/卸载插件 | **未实测** | 仅 import + 实例化 + `initialize()/terminate()`；未跑 StarManager 全流程 |
| 真实 NapCat 连接 / 真实 QQ 群 | **未实测** | 全部使用假传输层；无真实 QQ 号/群 |
| Windows 10/11 + AstrBot Launcher 生产部署 | **未实测** | 本机为 macOS |
| GroupGuardian 真实联动（安装+打补丁+真实消息） | **未实测** | 仅源码核对 + 真实注册表顺序用代理 Handler 断言；补丁内容有单元测试 |
| LivingMemory 真实联动（真实采集/画像） | **未实测 / 无法验证** | 仅上游源码静态阅读；无运行时追踪 |
| WebUI 在真实 Dashboard iframe 中的桥接 | **未实测** | 前端代码按 4.26+ 桥契约编写；无浏览器实测 |
| GitHub Actions CI | **已实测通过** | commit `e296b25`：3 个 job（3.12 / 3.13 单元测试 + 真实 AstrBot 4.29.4 框架测试）均 success；[run](https://github.com/Maxwell233/astrbot_plugin_group_approval/actions/runs/42675991825) |

---

## 10. 明确未验证（UNVERIFIED）事项

1. 真实 GroupGuardian 插件的加载/排序/执行（仅用真实 AstrBot 注册表 + 代理 Handler 复现顺序）。
2. LivingMemory 在真实 AstrBot 中的采集与画像生成（仅源码阅读）。
3. 真实 NapCat/QQ 的 API 行为、超时、风控对审批通知到达率的影响。
4. AstrBot StarManager 对插件的真实加载、热重载、并发消息分发。
5. WebUI 桥接在真实浏览器/Dashboard 中的表现。
6. GitHub Actions 已实际运行并通过（commit `e296b25`；[run](https://github.com/Maxwell233/astrbot_plugin_group_approval/actions/runs/42675991825)）；仍不代表真实 NapCat/QQ 环境已验证。
7. 真实 Windows 生产环境与 AstrBot Launcher 常驻行为。
