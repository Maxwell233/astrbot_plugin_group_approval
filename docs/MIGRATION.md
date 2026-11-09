# 0.1.2 双域数据迁移

首次启动时 SQLite V7 增加 `bot_settings`、`scope_policies`、`grants_v2`、`applications_v2`、`primary_group_levels` 和 `grant_restrictions`。数据库使用原数据目录，建议更新前做 SQLite 备份。

本项目此前尚未投入使用，新版从新的双域记录开始。旧表保留在数据库里，旧群授权、待审申请和群策略不参与新版 LLM 判定。没有将旧群授权自动扩大为同 Bot 的全群授权，也没有旧申请转换流程。已有审计表和通知 Outbox 沿用，新的记录由 `LLM_` 动作名或事件键标识。

配置主群后，请由用户重新申请或由 Owner 按域授予。旧版若已应用 LivingMemory 兼容补丁，升级前恢复其上游过滤器，否则该补丁所等待的旧闸门回调不再运行。
