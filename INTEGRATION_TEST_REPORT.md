# 0.1.1 开发调试版验证报告

PR #1 的审计基线为 `f50abbf2f690292c49a19aadc2ceba805b42302c`。本报告记录该分支的本轮修复。远程检查以 PR 当前提交为准。 [PR #1](https://github.com/Maxwell233/astrbot_plugin_group_approval/pull/1)

## 本地结果

| 范围 | 环境与结果 |
|---|---|
| 单元、服务及 SQLite 并发 | macOS，Python 3.12.16 和 3.13.17，各 **139 通过、1 跳过**。跳过项是未安装 AstrBot 时的整个框架测试模块。 |
| AstrBot 与 aiocqhttp 组件 | Python 3.13，AstrBot 4.29.4、aiocqhttp 1.4.4，**13 通过**。 |
| Dashboard | Node.js，**2 通过**。用简化 DOM 和请求替身测试审批、拒绝、撤销、拉黑和策略表格的 Bot 绑定。 |
| 静态检查 | `ruff check .`、`git diff --check`。 |
| 上游补丁 | 对 GroupGuardian `569f6bb1a0dd0bc77f2085ba8be9bdac72be3de5` 与 LivingMemory `1a216d78083af9fd80d72223ca7ead7e7100642d` 执行 `git apply --check` 及实际应用。加载已打补丁的 LivingMemory 上游过滤器，验证 AIOCQHTTP 延后调度和 Telegram 保留原路径。 |

复跑命令：

```sh
python -m pytest -q -p no:cacheprovider
ruff check .
node --test tests/dashboard_bot_scope.test.cjs
# 在独立环境安装 AstrBot==4.29.4 及测试依赖后
python -m pytest -q tests/test_framework_handler_order.py -p no:cacheprovider
```

## 缺陷与回归证据

| 缺陷 | 基线复现和修复证据 |
|---|---|
| GroupGuardian 优先级和误报 | 旧补丁能应用，真实装饰器却注册为 0。高优先级兄弟 Handler 也会掩盖真正入口。修订补丁注册为 100。真实 `StarRequestSubStage` 调用审核代理后执行真实闸门，拒绝消息未触达普通 Handler。另测停用、入口缺失和注册顺序变化。 |
| 申请和授权在网络查询期间过期 | 固定时钟跨过有效期，旧代码批准申请或允许已到期授权。新代码在最终事务取时，检查到期，并保留 `revision` 条件更新。 |
| Dashboard 写入和统计串 Bot | 七个接口用数字型离线 Bot 复现，现全部拒绝。申请还核对记录归属。概览计数及最近申请按选择过滤，前端操作绑定原记录的 Bot。 |
| 同实例多 QQ | 旧代码无法在冷启动时发现两个连接。真实 aiocqhttp 路由组件验证定向登录、发送和断线淘汰。消息传输使用测试 socket，没有登录 QQ。 |
| 跨平台和 LivingMemory | 真实 AstrBot 平台过滤器拒绝 Telegram 进入闸门。补丁后的被动采集仅在 AIOCQHTTP 准入通过时安排，拒绝事件未释放回调。 |
| 权限和隐私 | 跨 Bot 的 QQ 审批及历史查询拒绝。公开群回复不披露私有审核信息。全局封禁撤销该 Bot 在各群的授权，待审黑名单用户无法获批。 |
| SQLite 与通知 | 两个数据库和事务内创建的子任务复现了上下文误用。修复仍用独立事务连接。无身份的历史通知拒发，新审核通知禁止回退到申请来源群。 |

## 测试边界与部署验收

- AstrBot 测试覆盖真实注册表、装饰器、消息事件、处理阶段和 SDK。GroupGuardian 的具体审核、OCR、撤回及禁言使用代理处理器模拟或待实测。
- LivingMemory 的上游过滤器在应用补丁后做了组件验证。完整插件启动、后台采集、存储和画像未实测。
- CI 增加 Windows runner 上的 Python 3.12 单元/服务测试。Windows Launcher、实际 NapCat 反向 WebSocket 和真实 QQ 群未实测。
- 在测试群验证未授权、已授权及撤销后成员的文本、图片、链接、群通知和 AI 请求。验证不同 Bot 在同群的审批、统计、通知和切换。检查关闭或修补 LivingMemory 后的实际落库。排查会话过滤和 `SessionWaiter` 等在闸门前消费事件的路径。 [兼容性说明](COMPATIBILITY.md)
