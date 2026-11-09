# 0.1.2 开发调试版验证报告

基线 `15ca249`，目标 AstrBot `4.29.4`。部署验收与组件测试分别记录。

| 测试 | 本地结果 |
|---|---|
| Python 3.12 / 3.13 单元与 SQLite 服务测试 | 各 171 通过、2 跳过。跳过的是两个需要安装 AstrBot 的框架测试模块。 |
| Ruff 与补丁空白检查 | 通过 |
| Node Dashboard 测试 | 2 通过 |
| AstrBot 4.29.4 真实组件 | 25 通过。使用真实分发阶段、Handler、Agent 入口、事件对象及 aiocqhttp 路由。Provider、Agent 运行时和网络对端使用测试替身。 |
| 完整 AstrBot + NapCat + QQ 部署 | 未实测 |
| 真实签到、塔罗、今日小猪、GroupGuardian 和 LivingMemory 插件联动 | 未实测。独立 Handler 和 GroupGuardian 顺序使用代理 Handler 验证。 |

## 回归覆盖

| 场景 | 证据 |
|---|---|
| 默认群聊、唤醒、私聊和媒体输入 | `tests/test_native_llm.py` 以真实 ProcessStage 和 AgentRequestSubStage 验证。未授权事件未进入模拟运行时，因此没有原生请求、工具循环或会话历史。获批事件只检查一次，可执行多个模拟模型调用及流式步骤。唤醒标记由夹具构造。 |
| 普通插件与内容审核 | 使用真实 StarRequestSubStage 验证签到、塔罗、今日小猪代理先运行。GroupGuardian 代理先审核，默认聊天随后被拒。插件产生的真实 ProviderRequest 仍进入 Agent 入口。 |
| 主群、双域与跨域 | `tests/test_scope_service.py` 覆盖主群等级和变更、Bot 隔离、独立门槛与审批、两个跨域方向、主动申请、暂停恢复、撤销保护和两类豁免。 |
| SQLite 与迁移 | 实际 SQLite 并发申请与审批，通知失败时回滚申请、授权、审计及 Outbox。旧库升级保留历史且新授权表为空。 |
| 管理与多 Bot 安全 | 管理指令无需聊天授权，关闭开关后 QQ 管理指令禁用。Dashboard 路由测试核验管理员与当前 Bot，禁止跨 Bot 使用申请编号。Node 测试验证提交始终绑定原记录 Bot 和域。 |

## 复跑

```sh
python -m pytest -q -p no:cacheprovider
ruff check .
node --test tests/dashboard_bot_scope.test.cjs
# 在安装 AstrBot==4.29.4 的环境中
python -m pytest -q tests/test_framework_handler_order.py tests/test_native_llm.py -p no:cacheprovider
```

下一阶段在测试群验证实际 NapCat 成员查询、QQ 私聊通知、管理员撤回、真实工具 Agent 和第三方插件行为。核对 Dashboard 的兼容层状态后再开启原生聊天。 [部署说明](README.md#开始使用)
