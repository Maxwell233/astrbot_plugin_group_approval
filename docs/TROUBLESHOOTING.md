# 0.1.2 常见问题

- **Dashboard 显示原生聊天准入未启用**。确认插件已启用，AstrBot 版本为 4.29.4，启动日志没有兼容层错误。
- **无法保存主群**。确认所选 NapCat QQ 在主群中，能查询 Bot 自己的成员信息。
- **群聊可以，私聊不行**。GROUP 与 PRIVATE 各自申请和核验，默认分别要求主群 LV20 和 LV50。
- **已填写 Owner QQ，私聊仍提示无权限**。填写 QQ 后可使用管理指令。要让该 Owner 直接聊天，在 Group Approval Dashboard 选择对应 Bot，勾选「Owner 原生聊天豁免」并保存。若只想开通私聊，可在同一页面手动授予 PRIVATE。
- **PRIVATE 已显示 ACTIVE，私聊仍提示尚无权限**。私聊发送 `/我的权限`，把返回的 Bot QQ 和你的 QQ 分别与 Dashboard 当前选中的 Bot 和 PRIVATE 授权行核对。如果两组都一致，刷新 Dashboard 核对状态，并确认正在使用的 Bot 和 Dashboard 属于同一个 AstrBot 实例。
- **发送申请指令没有回复**。确认当前会话启用了 Group Approval。如果 AstrBot 设置为私聊必须使用唤醒前缀，请把实际配置的前缀加在申请指令名前，例如 `!申请私聊`。
- **等级恢复后仍提示无权限**。暂停的授权通过主动申请目标域恢复。若被人工撤销，请联系 Owner 重新授予。
- **独立插件仍产生 Token 消耗**。其独立 LLM、LivingMemory 后台总结等调用由相应插件配置控制。Group Approval 只限制默认聊天。
- **GroupGuardian 或 LivingMemory 行为改变**。检查是否仍安装了旧版依赖消息闸门的采集补丁，并核对独立插件的群规则。 [兼容性说明](../COMPATIBILITY.md)
