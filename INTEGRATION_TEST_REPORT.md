# 0.1.2 开发调试版验证报告

PR #2 审计基线 `9d46db42ebe68889e55a7e3f5fe36a3a5d0f683b`，目标 AstrBot `4.29.4`。

| 测试 | 本地结果 |
|---|---|
| Python 3.12 / 3.13 单元与 SQLite 服务测试 | 各 182 通过、3 跳过。跳过的模块需要真实 AstrBot。 |
| Ruff 与补丁空白检查 | 通过 |
| Node Dashboard 测试 | 2 通过 |
| AstrBot 4.29.4 真实组件 | 35 通过。真实分发、事件、Handler、Agent 阶段、第三方流式结果和卸载过程；模型、网络及测试中的 Agent 运行器使用替身。 |
| 完整 AstrBot + NapCat + QQ 部署 | 未实测 |
| 真实签到、塔罗、今日小猪、GroupGuardian 和 LivingMemory 插件联动 | 未实测。用代理 Handler 验证独立功能和审核的分发。 |

## 回归覆盖

本轮针对四项修复新增 26 个核心用例，包含参数化场景。首次安装清理后，SQLite、授权状态、管理权限、通知隐私和 Dashboard 隔离测试改用当前双域入口。旧群级闸门、旧库升级、旧授权有效期及群通知回退用例随对应功能退役。

| 场景 | 证据 |
|---|---|
| 安装、注销与重载 | `tests/test_guard_lifecycle.py` 覆盖重复初始化、内外包装的加载顺序、多次重载、旧回调释放、外部替换诊断、鉴权取消及真实插件关库顺序。 |
| 默认聊天与流式输出 | `tests/test_native_llm.py` 验证未授权请求不会进入历史、工具和模型请求路径，Agent 多步执行只核验一次。真实本地 Agent 子阶段验证进入构建器的条件，真实第三方 Agent 子阶段使用替身 Dify 运行器验证流式结果、正常关闭及拒绝后零调用。 |
| 独立插件与审核 | 真实 `StarRequestSubStage` 运行签到、塔罗、今日小猪、GroupGuardian 及独立模型插件的代理 Handler。插件显式 `ProviderRequest` 继续分发，`plugin_set` 无法跳过默认聊天核验。 |
| 黑名单与人工撤销 | `tests/test_scope_service.py` 覆盖 ACTIVE、SUSPENDED、REVOKED 与无授权，双域限制、解禁重新核验、在途缓存和审计失败回滚。 |
| 查询限频 | 资格不足或查询失败均计数，重复已授权或待审申请直接返回状态，跨 Bot/域隔离，并发受限。独立验证每小时新建待审限额。 |
| 主群变更 | 旧申请取消并记录原因，旧编号审批失败。覆盖变更与审批并发、切回原群、旧快照失效、已有授权保留及审计写入失败回滚。 |
| 首次安装与管理 | SQLite 初始结构、重复初始化、并发唯一性、审批与审计/Outbox 回滚，管理权限与聊天权限分离，Dashboard 的身份及 Bot 绑定，私聊通知与重试。 |

## 复跑

```sh
python -m pytest -q -p no:cacheprovider
ruff check .
node --test tests/dashboard_bot_scope.test.cjs
# 在安装 AstrBot==4.29.4 的环境中
python -m pytest -q -p no:cacheprovider tests/test_framework_handler_order.py tests/test_native_llm.py tests/test_guard_lifecycle.py
```

安装验收需验证实际 NapCat 成员查询、QQ 私聊通知、真实工具 Agent、第三方插件与模型服务。在启用原生聊天前核对 Dashboard 的准入状态。 [部署说明](README.md#开始使用)
