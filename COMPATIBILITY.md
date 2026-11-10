# 0.1.2 兼容性

## AstrBot 原生聊天边界

AstrBot 4.29.4 的 `ProcessStage.process()` 首先运行 `StarRequestSubStage`。插件若返回 `event.request_llm()` 生成的 `ProviderRequest`，框架把它放入事件 `provider_request` 后调用 `AgentRequestSubStage`。默认唤醒分支也调用同一子阶段，但没有这个请求对象。两个 Agent 路径都会调用 `on_llm_request`，而 `on_waiting_llm_request` 在本地运行时内，无法作为统一来源标记。 [框架分发源码](https://github.com/AstrBotDevs/AstrBot/blob/v4.29.4/astrbot/core/pipeline/process_stage/stage.py)

0.1.2 在 **4.29.4** 进程中只安装一个 Group Approval 包装。重载会重新注册授权回调。卸载先停止新鉴权，再等待在途鉴权结束并关闭 SQLite。框架方法仍直接指向本插件包装时才恢复原方法，第三方外层包装会保留。包装在本地和第三方运行时准备请求前检查默认 QQ 聊天。保留独立插件携带的 `ProviderRequest`，并避免同一事件随后再进入默认聊天。独立插件直接调用 Provider 也绕过这个阶段。拒绝只发送普通提示，不调用 `stop_event()`，不创建原生 Agent 的历史或工具运行。框架在更早阶段可能运行独立处理，例如平台转录、事件过滤器或 `SessionWaiter`。这些路径的消耗由其自身配置控制。

版本不匹配时插件拒绝安装兼容层。启用原生聊天前确认 Dashboard 显示准入已启用。若其他插件在运行期间接管 Agent 方法，Dashboard 显示 `EXTERNAL_METHOD_UNVERIFIED`，日志会告警。外层调用本插件包装时仍会执行鉴权；若外层完全取代原调用链，Group Approval 无法拦截。需排查方法替换后再启用原生聊天。插件禁用或卸载后，残留在外层调用链中的包装透明转发。AstrBot 重载会先卸载插件再初始化新实例。需要连续限制聊天时，先暂停原生聊天，待新实例显示准入已启用后再开放。

## GroupGuardian 与 LivingMemory

GroupGuardian 仍在普通消息阶段审核全体适用成员。旧的优先级补丁可保留以维持它相对其他普通插件的顺序，0.1.2 的 LLM 准入已不依赖该补丁。实际审核和处罚仍取决于规则和 QQ 群权限。

LivingMemory 原版过滤器继续按其配置采集消息。首次安装使用其原版过滤器即可。后台总结、直接调用 Provider 的插件与该插件自主调用的 Agent 请求都不在本项目的 Token 准入范围。 [旧补丁处理](patches/README.md)

## NapCat 与缓存

成员查询通过指定 OneBot 账号发送 `no_cache=True`。插件使用当前主群和当前用户返回的身份及等级。NapCat 版本自身可能回退到协议端缓存，退出主群的发现时点还受配置的等级缓存 TTL 影响。过期的插件快照在查询失败时不会继续用于授权。
