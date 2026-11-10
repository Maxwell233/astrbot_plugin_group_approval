# 第三方兼容补丁

这些补丁在目标插件目录中手动应用。先暂停受管 Bot 的群消息接入，备份目标文件，再核对上游提交和补丁。AstrBot 的插件更新可能覆盖补丁，更新后需要重新检查。

## GroupGuardian 主审核优先级

已核对 [GroupGuardian v2.8.11](https://github.com/zcj-ui/astrbot_plugin_group_guardian/tree/569f6bb1a0dd0bc77f2085ba8be9bdac72be3de5)。上游主审核入口是 `main.py` 的 `_handle_message`，默认优先级 0。AstrBot 4.29.4 用最靠近函数的装饰器完成首次注册。补丁调整装饰器顺序，让带 `priority=100` 的事件装饰器先注册，同时保留 AIOCQHTTP 过滤器。

在 GroupGuardian 插件目录执行，替换补丁路径。

```sh
git apply --check "/path/to/group-approval/patches/group_guardian_priority.patch"
git apply "/path/to/group-approval/patches/group_guardian_priority.patch"
```

重载两插件或重启 AstrBot，再打开 Group Approval 的诊断。确认主审核入口为 100、准入为 50，且主审核在前。早期补丁把优先级写在外层装饰器。若装过早期补丁，先恢复上游 `_handle_message` 的原始装饰器，再应用当前补丁并复核注册表。

GroupGuardian © 2025 zhaisir，采用 MIT 许可。补丁涉及的上游文件保留原许可证。 [上游源码与许可](https://github.com/zcj-ui/astrbot_plugin_group_guardian/tree/569f6bb1a0dd0bc77f2085ba8be9bdac72be3de5)

## LivingMemory 被动采集

若无需全群采集，把 LivingMemory 的 `session_manager.enable_full_group_capture` 设为 `false`。若需要保留受管 QQ 群中已准入消息的被动采集，先在测试环境验证 [LivingMemory 2.10.0](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/tree/1a216d78083af9fd80d72223ca7ead7e7100642d) 的兼容补丁。

```sh
git apply --check "/path/to/group-approval/patches/livingmemory_admission.patch"
git apply "/path/to/group-approval/patches/livingmemory_admission.patch"
```

补丁把 AIOCQHTTP 的采集调度延后到 Group Approval 的群消息准入通过时。准入未运行、禁用、拒绝或处理准入专用指令时，采集保持关闭。其他平台沿用 LivingMemory 原路径。补丁覆盖该版本的被动采集入口。历史记录、主动记忆工具、其他来源和按会话共享的记忆另行检查。完整插件和实际落库仍待联调。

LivingMemory 衍生补丁 `livingmemory_admission.patch` 按上游 AGPL-3.0 许可提供。 [对应上游源码与许可](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/tree/1a216d78083af9fd80d72223ca7ead7e7100642d)

## 0.1.2 状态

新版 LLM 准入位于普通消息 Handler 之后，GroupGuardian 优先级补丁无需为新版单独安装，已安装的可以保留。LivingMemory 补丁依赖 0.1.1 的全消息闸门。0.1.2 请直接使用上游 LivingMemory。若曾应用该补丁，请先恢复其原文件或在对应插件目录用 `git apply --reverse` 撤销，再重载。
