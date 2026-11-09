# AstrBot 插件兼容性与架构调研报告 (COMPATIBILITY.md)

**生成时间：** 2026-11-09  
**插件名称：** `astrbot_plugin_group_approval`（群等级准入与管理员审批）  
**目标运行时：** AstrBot v4.x+, NapCat OneBot v11, Python 3.12+ (Windows 10/11 & macOS/Linux)

---

## 1. 调研基准与固定版本信息

| 组件 / 项目 | 仓库与文档 | 参照/测试版本 | 核心依赖规范 |
|---|---|---|---|
| **AstrBot** | [AstrBotDevs/AstrBot](https://github.com/AstrBotDevs/AstrBot) | v4.16+ (Star v2 规范) | Python 3.12, Quart, Pydantic, aiosqlite |
| **NapCatQQ** | [NapNeko/NapCatQQ](https://github.com/NapNeko/NapCatQQ) | OneBot v11 反向 WebSocket | API: `get_group_member_info`, `send_private_msg` |
| **GroupGuardian** | [zcj-ui/astrbot_plugin_group_guardian](https://github.com/zcj-ui/astrbot_plugin_group_guardian) | 最新稳定版 | `@filter.event_message_type(..., priority=100)` |
| **LivingMemory** | [lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory) | v2.9.1+ | `@filter.on_llm_request`, 用户级向量作用域 |
| **Jrys / Rollpig / Cyber Divination** | 常见娱乐/工具插件 | 标准 Star 插件规范 | `@filter.command(..., priority=0)` |

---

## 2. 核心技术问题调研与验证结论

### 2.1 AstrBot 插件注册与生命周期规范 (Star SDK)
- **基类与自动发现**：插件主类继承 `astrbot.api.star.Star`。无需强制依赖旧版 `@register`，AstrBot 通过 `Star` 子类及 `metadata.yaml` 自动发现与加载。
- **构造签名**：`def __init__(self, context: Context, config: AstrBotConfig = None)`。
- **持久化数据目录**：
  - 严禁使用废弃的 `self.context.get_data_dir()`（在新版中已引发 `AttributeError`）。
  - **标准写法**：调用 `astrbot.api.star.StarTools.get_data_dir()`，返回 `pathlib.Path`，确保插件更新或重装时数据安全保留在 `data/plugins/...`。
- **生命周期清理**：在 `Star` 类中实现 `async def terminate(self)`，在插件卸载、重载或主进程退出时安全取消后台轮询任务并关闭 SQLite 连接池。

### 2.2 消息 Handler 优先级机制
- AstrBot 装饰器（`@filter.event_message_type`、`@filter.command` 等）均支持 `priority` 参数。
- **数值与执行顺序**：`priority` 数值越高，执行优先级越高。默认优先级为 `0`。
- **设计布局**：
  - **GroupGuardian**（群安全审核）：`priority = 100`（最先执行，确保全群无差别审查）。
  - **Group Approval Gate**（准入拦截闸门）：`priority = 50`（位于审核插件之后、业务插件之前）。
  - **第三方普通插件**（签到、塔罗、小猪、AI 会话）：`priority = 0` 或更低。

### 2.3 `event.stop_event()` 的真实执行边界
- **生效范围**：
  - 调用 `event.stop_event()` 会立即终止 AstrBot 事件分发流水线后续的所有低优先级 Handler。
  - 后续的指令匹配、普通插件逻辑、以及 AstrBot Agent 底层的 LLM 对话请求全部被阻断。
- **非拦截范围**：
  - 不会撤销此前已经执行的高优先级 Handler（因此 GroupGuardian 在 priority=100 执行的内容审核不受影响）。
  - 不会直接终止外部 HTTP API 或插件主动发起的定时后台任务。因此，管理指令与 WebUI 接口必须使用服务层独立的 `check_access` / `check_admin` 进行显式鉴权。

### 2.4 内置群聊历史与上下文机制
- AstrBot 的会话上下文和历史消息记录发生在消息分发及 LLM 管道中。在 priority=50 时调用 `event.stop_event()` 可以阻止消息进入 AstrBot Agent 的 LLM 上下文缓存，避免未获批用户的发言污染 AI 提示词。

### 2.5 GroupGuardian 兼容性保障
- **核心原则**：权限插件绝不能替代或阻止全群内容审查。
- **上游优先级与调度架构说明**：
  - **背景**：上游 `GroupGuardian` 主审核 Handler 若未显式声明优先级，在 AstrBot 中默认优先级为 0。若 Group Approval 运行于 `priority = 50`，可能导致未授权用户的违规发言在被 GroupGuardian 审核前先被准入闸门拦截。
  - **AstrBot 调度机制真相**：AstrBot 在插件加载注册阶段通过 Handler 的 `extras_configs["priority"]` 进行排序。如果在运行时动态修改 Handler 实例上的属性（如 `handler.priority = 100`），由于内部事件分发链已排定，该修改无法生效。因此必须移除非官方标准的运行时修改逻辑。
  - **上游标准兼容补丁**：
    在 `zcj-ui/astrbot_plugin_group_guardian` 的主过滤装饰器中显式指定 `priority=100`：
    ```python
    @filter.event_message_type(
        filter.EventMessageType.ALL,
        priority=100,
    )
    ```
  - **内置运行期 Handler 诊断服务 (HandlerDiagnostics)**：
    Group Approval 提供只读诊断服务 `HandlerDiagnostics`，供 WebUI 或运维 API 调用，读取各 Handler 真实的 `extras_configs["priority"]`。若检测到 GroupGuardian 优先级 $\le 50$，即输出明确风险告警并给出补丁指引。
- **执行时序保证**：
  ```text
  群成员发言
     ↓
  GroupGuardian (priority=100，显式补丁配置)
     ↓ 违规检测：撤回/禁言/告警
     ↓ 未违规或审核完毕后向下传递
  Group Approval Gate (priority=50)
     ↓ 检查群策略与准入授权
     ├─ 已授权 / 豁免：放行至普通业务 (priority=0)
     └─ 未授权：调用 event.stop_event()，终止后续业务
  ```
- **结论**：未授权成员发送违规内容时，GroupGuardian 依然先完成审核；Group Approval 仅阻断其调用 Bot 业务的能力。

### 2.6 LivingMemory 记忆隔离保障
- `astrbot_plugin_livingmemory` 介入时机为 `@filter.on_llm_request`。
- 当 Group Approval 在消息管道入口调用 `stop_event()` 时，LLM 请求根本不会发起，因此 `on_llm_request` 钩子不会被触发，未授权用户无法生成个人画像或长期向量记忆。
- 建议运行环境配置：`memory_scope_mode = user`，`enable_full_group_capture = false`。

### 2.7 第三方娱乐插件 (Jrys / Cyber Divination / Rollpig)
- 经分析，第三方插件主要使用 `@filter.command`，依赖 AstrBot 默认优先级（0）。
- 部分插件支持无前缀短语（如 `今日小猪`、`今日运势`、`塔罗`）或 `@Bot`。
- Group Approval 的准入网关使用 `@filter.event_message_type(filter.EventMessageType.ALL, priority=50)`，对受管群的所有消息在第一入口处集中鉴权，无论是命令前缀、纯文本还是图片，未授权用户均无法穿透。

### 2.8 AstrBot WebUI / Plugin Pages API
- **静态资源规范**：放置在插件根目录 `pages/<view_name>/`（如 `pages/dashboard/index.html`）。同时创建 `views/` 保证向下兼容。
- **前端 SDK**：AstrBot 自动注入 `/api/plugin/page/bridge-sdk.js`，前端通过 `window.AstrBotPluginPage` 或 `window.AstrBotPluginView` 调用 `await bridge.ready()` 和 `bridge.apiGet(...)` / `bridge.apiPost(...)`。
- **后端 API 注册**：在 `__init__` 中调用 `context.register_web_api(f"/{PLUGIN_NAME}/...", handler, methods, description)`。
- **API 路径映射**：前端请求经 AstrBot 核心转发至 `/api/plug/<plugin_name>/<endpoint>`。

### 2.9 NapCat OneBot v11 群等级字段与协议特性
- **API 路径**：`event.bot.api.call_action("get_group_member_info", group_id=..., user_id=..., no_cache=True)`。
- **等级字段**：
  - 返回对象中的 `data.level` 代表群内活跃/头衔等级（对应 `memberRealLevel`）。
  - 类型多为字符串形式（如 `"10"`、`"0"`、`"LV18"`），必须使用正则表达式严格提取整数。
  - **严禁使用 `qq_level`**（此字段为 QQ 账号等级，非群成员等级）。
  - 若 `data.level` 缺失、解析失败或群成员已退群（返回错误码），严格判定为不满足准入条件。
- **缓存策略**：申请及审批时设置 `no_cache: True` 强制穿透至腾讯服务器；日常决策使用本地 300 秒 TTL 缓存与并发防抖合并。
