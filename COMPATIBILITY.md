# AstrBot 插件兼容性与架构调研报告 (COMPATIBILITY.md)

**更新时间：** 2026-11-09（基于修复后的当前工作树；本文不描述已废弃的旧实现）
**插件：** `astrbot_plugin_group_approval` v0.1.1（**开发调试版**，非稳定版 Release）
**目标运行时：** **AstrBot >= 4.26.0** + NapCat OneBot v11 + Python 3.12+
**支持平台：** 仅 `aiocqhttp`（见 `metadata.yaml`）

**依据（可复核）：**

| 证据源 | 说明 |
|---|---|
| `upstream/AstrBot`（tag **v4.29.4**，commit `6133541`） | AstrBot 框架源码只读核对（优先级、`stop_event`、注册表、Plugin Web API） |
| `.venv-astrbot`（PyPI **AstrBot 4.29.4** 实际安装） | **真实框架**导入与注册表探测，由 `tests/test_framework_handler_order.py` 覆盖 |
| `tests/test_framework_handler_order.py` | 真实 AstrBot 4.29.4 下的注册表、处理阶段和 OneBot SDK 路由测试（见集成测试报告） |
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
| **LivingMemory** | 上游 2.10.0（AGPL-3.0） | 关闭原版被动采集，或应用兼容补丁后按准入调度 | 见第 6 节 |

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
该顺序由真实 AstrBot 4.29.4 测试 `tests/test_framework_handler_order.py` 断言。

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

## 5. GroupGuardian 主审核顺序

对 [GroupGuardian v2.8.11](https://github.com/zcj-ui/astrbot_plugin_group_guardian/tree/569f6bb1a0dd0bc77f2085ba8be9bdac72be3de5) 验证了补丁的适用性。AstrBot 4.29.4 在最靠近函数的装饰器首次登记优先级。旧补丁写在外层，实际仍为 0。当前补丁把带 `priority=100` 的事件装饰器放到内层，平台过滤器照常注册。详见 [应用步骤](patches/README.md)。

诊断读取启用的 AdapterMessageEvent 顺序，按所属插件、完整注册名与平台/事件过滤器定位主审核与准入闸门。`VERIFIED` 仅说明主审核入口的真实注册位置和优先级均在准入前。`CONFLICT` 表示阻断级顺序冲突。入口禁用、未安装或无法确认分别报告 `DISABLED`、`NOT_INSTALLED` 或 `UNVERIFIED`。热重载后重新打开诊断以读取当前注册表，并检查 `other_handlers_before_gate` 中可能提前运行的处理器。

若部署要求全群审核，先暂停接入，完成补丁、重载和测试群复核后恢复。AstrBot 配置和会话过滤可能移除任一入口，GroupGuardian 自身也可按群、用户豁免或缺少 QQ 群管理权限而跳过审核。

## 6. LivingMemory 被动采集边界

[LivingMemory 2.10.0](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/tree/1a216d78083af9fd80d72223ca7ead7e7100642d) 的 `PassiveGroupCaptureFilter.filter()` 在 `WakingCheckStage` 调度后台任务。它返回 `False`，所以即使对应 Handler 从未执行，采集仍可能开始。准入 Handler 的 `stop_event()` 无法撤销已调度任务。

- 禁用方式：`session_manager.enable_full_group_capture=false`。按群限制可使用 LivingMemory 的 `access_control.whitelist_enabled` 与 `allowed_ids`。同群用户需要逐人授权时，单靠群白名单无法隔离。
- 兼容方式：[LivingMemory 补丁](patches/README.md#livingmemory-被动采集)在 AIOCQHTTP 上将采集交给准入通过后的回调。未运行准入、被拒绝、仅处理准入命令及 GroupGuardian 提前停止的事件均不会由此入口采集。若在一个适配器实例上接入多个 QQ 账号，LivingMemory 的会话与历史库隔离仍须单独检查。
- 独立任务、主动记忆工具、历史库、总结和 LLM 召回各有独立路径。应用补丁后应按权限需求核查这些路径和过去已入库的数据。

AstrBot 4.29.4 的 `ProcessStage.process()` 可在 `StarRequestSubStage` 之前调用 `SessionWaiter.trigger()` 并消费消息。对受管会话，停用依赖该路径的第三方功能，或在其回调内部执行同等准入检查。其他自定义过滤器也可能在闸门前产生副作用。

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

## 8. Bot 身份与多账号路由

- 同一个 AstrBot AIOCQHTTP 实例可以有多个反向 WebSocket 账号。插件用 aiocqhttp 1.4.4 的连接表枚举候选 QQ，然后逐账号用 `get_login_info(self_id=QQ)` 核验。缓存按实例 ID 和 QQ 分开，断线或 socket 替换时失效。
- OneBot 事件的 `self_id` 源自协议端负载。反向连接身份由定向探测验证。仅对不公开连接表的旧客户端保留短时事件候选。所有 Dashboard 写操作都要通过新探测。
- `/bots` 的 `verified_bots` 用于写操作。历史记录和聚合读取是独立的。应用列表与概览均携带记录所属 Bot。

## 9. 实测范围

已在 macOS 验证 Python 3.12/3.13、AstrBot 4.29.4、aiocqhttp 1.4.4 和两个上游补丁。Windows CI 仅覆盖单元/服务测试，Windows Launcher 常驻、真实 NapCat、QQ 群内容处置及第三方插件完整联动仍需部署验收。 [测试报告](INTEGRATION_TEST_REPORT.md)
