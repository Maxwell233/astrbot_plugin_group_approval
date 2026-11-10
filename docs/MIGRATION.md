# 0.1.2 首次安装与数据存储

首次启动在 AstrBot 分配的插件数据目录创建 `group_approval.db`。初始化可重复执行，保留该结构下的已有配置、授权、审核记录和通知队列。

`bot_settings` 与 `scope_policies` 保存每个 Bot 的主群和策略。`grants` 和 `applications` 保存 GROUP / PRIVATE 授权与申请。`primary_group_levels` 保存主群资格快照，`grant_restrictions` 保存各域的黑名单和人工撤销。审核员覆盖、限频、审计与通知 Outbox 分表保存。

项目此前未投入使用，安装时从空数据目录开始。若本地曾运行旧开发构建，请另存其数据库，使用空目录安装当前版本。配置主群后，由用户主动申请或由 Owner 按域授予。
