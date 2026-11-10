# 第三方兼容补丁

0.1.2 的原生聊天准入在普通消息处理之后执行。首次安装可直接使用上游 GroupGuardian 和 LivingMemory。

## 可选的 GroupGuardian 主审核优先级

已核对 [GroupGuardian v2.8.11](https://github.com/zcj-ui/astrbot_plugin_group_guardian/tree/569f6bb1a0dd0bc77f2085ba8be9bdac72be3de5)。上游主审核入口是 `main.py` 的 `_handle_message`，默认优先级 0。AstrBot 4.29.4 用最靠近函数的装饰器完成首次注册。补丁调整装饰器顺序，让带 `priority=100` 的事件装饰器先注册，同时保留 AIOCQHTTP 过滤器。

在 GroupGuardian 插件目录执行，替换补丁路径。

```sh
git apply --check "/path/to/group-approval/patches/group_guardian_priority.patch"
git apply "/path/to/group-approval/patches/group_guardian_priority.patch"
```

重载 GroupGuardian 或重启 AstrBot。若曾安装把优先级写在外层装饰器的早期补丁，先恢复上游 `_handle_message` 的原始装饰器，再应用当前补丁。

GroupGuardian © 2025 zhaisir，采用 MIT 许可。补丁涉及的上游文件保留原许可证。 [上游源码与许可](https://github.com/zcj-ui/astrbot_plugin_group_guardian/tree/569f6bb1a0dd0bc77f2085ba8be9bdac72be3de5)

## 撤销旧版 LivingMemory 补丁

旧补丁依赖已退役的 0.1.1 群消息闸门。曾应用过补丁的部署需要恢复 LivingMemory 原文件，或在对应插件目录检查并撤销补丁，然后重载 LivingMemory。

```sh
git apply --reverse --check "/path/to/group-approval/patches/livingmemory_admission.patch"
git apply --reverse "/path/to/group-approval/patches/livingmemory_admission.patch"
```

保留此补丁供撤销使用。LivingMemory 衍生补丁按上游 AGPL-3.0 许可提供。 [对应上游源码与许可](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/tree/1a216d78083af9fd80d72223ca7ead7e7100642d)
