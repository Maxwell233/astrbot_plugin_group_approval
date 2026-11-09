# AstrBot Group Approval

[![Tests](https://github.com/Maxwell233/astrbot_plugin_group_approval/actions/workflows/tests.yml/badge.svg)](https://github.com/Maxwell233/astrbot_plugin_group_approval/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**0.1.2 开发调试版** · Python 3.12+ · AstrBot **4.29.4** · NapCat OneBot v11

## 项目背景

这个插件是为在规模较大的 QQ 群里使用 AstrBot 而开发的。群成员多，人人都能直接触发 AI 对话时，刷指令和重复聊天容易占用模型额度，也会打扰正常群聊。群成员达到指定主群的活跃等级后，可以主动申请 Bot 的 AI 聊天权限，管理员可按需启用人工审批。

审批只管 **AstrBot 原生 AI 聊天**。签到、塔罗、今日小猪、GroupGuardian 审核和其他独立插件仍按各自规则工作。独立插件自发调用 LLM，以及 LivingMemory 后台总结，也由各插件自己的配置管理。

## 两种授权

| 域 | 可以做什么 | 默认门槛 | 默认人工审批 |
|---|---|---:|---|
| GROUP | 在同一个 QQ Bot 所在的群里使用原生 AI 聊天 | 主群 LV10 | 开启 |
| PRIVATE | 与该 QQ Bot 私聊进行原生 AI 聊天 | 主群 LV20 | 开启 |

每个 QQ Bot 单独配置一个主群。申请、批准和后续资格复核都查主群。获批的 GROUP 用户在其他群聊天时仍使用这个主群的等级。两个授权域各自撤销和恢复，授权没有到期日。等级缓存默认 300 秒。

可独立开启 GROUP → PRIVATE 和 PRIVATE → GROUP 的免审批方向。用户须主动申请目标域，满足目标等级，且来源授权仍有效。跨域授予不继承豁免，也不会恢复人工撤销的权限。

## 开始使用

1. 将插件安装到 AstrBot 4.29.4 的插件目录，配置 Owner QQ，确认 NapCat 账号已连接。
2. 打开 Group Approval Dashboard，确认显示「原生聊天准入已启用」。选择 QQ Bot，保存主群和两个授权域的策略。Bot 须已加入主群并能查询群成员等级。
3. 用测试群账号分别试用未授权的原生聊天和独立插件，再申请并测试 GROUP、PRIVATE。主群变更后第一次使用会重新核验。
4. GroupGuardian 可按原配置审核全群。旧 GroupGuardian 优先级补丁仍可保留。若旧版应用过 LivingMemory 被动采集补丁，请恢复其上游过滤器再重载。 [兼容性与验证范围](COMPATIBILITY.md)

## 指令

群聊和私聊都能发送 `/申请群聊`、`/申请私聊`、`/我的权限` 和 `/取消申请 群聊|私聊`。在群聊使用 `/申请使用` 默认申请 GROUP，在私聊默认申请 PRIVATE。`/申请状态` 在私聊查看。含敏感内容的申请理由请通过私聊提交。

管理员在 Bot 私聊使用 `/ga`。常用命令包括：

| 用途 | 指令 |
|---|---|
| 查看和审批 | `/ga pending [group\|private]`、`/ga approve <编号>`、`/ga reject <编号> [原因]` |
| 授予、撤销、恢复 | `/ga grant <QQ> <group\|private>`、`/ga revoke <QQ> <group\|private\|all>`、`/ga restore <QQ> <group\|private>` |
| 主群和策略 | `/ga primary-group set <群号>`、`/ga primary-group show`、`/ga level <group\|private> <等级>`、`/ga approval <group\|private> <on\|off>` |
| 跨域免审 | `/ga cross-grant <group-to-private\|private-to-group> <on\|off>` |
| 豁免 | 授予时可加 `--level-exempt`、`--membership-exempt`，也可用 `/ga exemptions <QQ> <域> <等级 on/off> <成员 on/off>` |
| 查询 | `/ga user <QQ>`、`/ga status` |

Owner 与 Reviewer 的管理资格不自动授予 AI 聊天。两类豁免由 Owner 分别授予或解除。只有双重豁免可以完全跳过主群资格核验。Dashboard 也可查看申请、授权来源、最近等级核验和审计记录。

- [配置说明](docs/CONFIGURATION.md)
- [数据迁移](docs/MIGRATION.md)
- [安全边界](docs/SECURITY.md)
- [组件测试与验收](INTEGRATION_TEST_REPORT.md)
