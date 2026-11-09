# GroupGuardian 兼容补丁（内容审核优先级）

## 为什么需要这个补丁

本插件的准入闸门以 `priority=50` 注册，用于在普通业务插件（默认 `priority=0`）之前拦截
未授权用户。AstrBot 的调度顺序是 **priority 数值越大越先执行**，优先级存放在
`handler.extras_configs["priority"]`（默认 0），相同优先级按注册/加载顺序稳定排序。

上游 [GroupGuardian](https://github.com/zcj-ui/astrbot_plugin_group_guardian) 的主审核
Handler `_handle_message` **没有声明 `priority`**，即默认 0。因此在未打补丁的部署中：

```text
未授权用户违规消息
  → Group Approval 闸门 (priority=50) 先执行 → stop_event()
  → GroupGuardian 审核 (priority=0) 根本不会运行
```

这会让未授权用户的色情 / 诈骗 / 广告 / 违法推广内容逃过审核，属于本插件 v0.1.1 的
P0 缺陷（BUG-001）。AstrBot 没有“审核槽位”或强制顺序的官方机制，也不允许一个插件安全地
在运行时修改另一个插件的 Handler 状态，因此正确的修复是**给 GroupGuardian 的主审核 Handler
显式提高优先级**。

## 补丁内容

`group_guardian_priority.patch` 只修改上游 `main.py` 中**主审核 Handler** 的装饰器：

```diff
-    @filter.event_message_type(filter.EventMessageType.ALL)
+    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
     @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
     async def _handle_message(self, event: AiocqhttpMessageEvent):
```

`100 > 50`，因此审核先于准入闸门执行；其它 Handler（入群请求、名片、禁言、35 个命令、
22 个 LLM 工具、`on_llm_request`）均不变。

## 部署方式

```bash
# 在 GroupGuardian 插件目录内执行（备份 main.py 后）
git apply --check /path/to/astrbot_plugin_group_approval/patches/group_guardian_priority.patch
git apply         /path/to/astrbot_plugin_group_approval/patches/group_guardian_priority.patch
```

或手动把主审核 Handler 的装饰器改为 `priority=100`。

补丁基于 GroupGuardian v2.8.11（commit `569f6bb1a0dd0bc77f2085ba8be9bdac72be3de5`）。
上游升级后若上下文变化，`git apply` 会失败而不会错误修改，请按当前源码手动调整。

## 验证

- 插件启动时会读取 AstrBot 真实 Handler 注册表并在日志中报告：
  `VERIFIED` / `CONFLICT` / `NOT_INSTALLED` / `UNVERIFIED`。
- WebUI Dashboard 的 `diagnostics` 接口返回同样结论（只读）。
- `CONFLICT` 表示补丁未生效，此时**不保证**未授权用户内容被审核；插件不会谎报安全。

## 许可证与维护

GroupGuardian 使用 **MIT License**（Copyright (c) 2025 zhaisir，
<https://github.com/zcj-ui/astrbot_plugin_group_guardian>）。本补丁仅包含对其源码的最小
修改，遵守 MIT 许可并保留上游版权声明。本补丁维护在本仓库 `patches/` 下，不修改上游仓库；
建议向上游提交同等补丁或在部署说明中要求使用打过补丁的版本。

LivingMemory 使用 AGPL-3.0，本仓库**不包含**对其源码的任何修改或衍生代码。
