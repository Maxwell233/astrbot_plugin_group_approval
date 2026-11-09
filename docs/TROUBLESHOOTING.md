# 故障排除指南 (TROUBLESHOOTING.md)

适用于 `astrbot_plugin_group_approval` **v0.1.1**（开发调试版）。排查前请先确认：

- AstrBot **>= 4.26.0**（旧版 AstrBot 会导致 `astrbot.api.web` 导入失败，插件退化为无操作的 stub；
  旧文档中的 4.16+ 声明已废弃）；
- 插件目录为 `astrbot_plugin_group_approval`，前端资源只在 **`views/dashboard/`**（`pages/dashboard/` 已删除）。

---

## 1. 插件加载后没有任何拦截效果 / 日志提示 stub

### 现象
群消息不被拦截，`/ga` 无响应，WebUI 无路由。

### 排查
1. 确认 AstrBot 版本 **>= 4.26.0**。若低于该版本：`main.py` 的 `from astrbot.api.web import ...` 会失败，
   `ASTRBOT_AVAILABLE=False`，`filter` 退化为空装饰器 → **不会注册任何 Handler 与 Web 路由**。
2. 查看 AstrBot 日志是否有 `astrbot.api.web` 相关 ImportError。
3. 确认插件配置 `enabled=true`，且目标群已在 WebUI「群组策略」中启用（`default_policy=deny` 时未配置群会被拒绝）。

---

## 2. WebUI 看板 404 / 空白 / 无法加载

1. 确认 AstrBot **>= 4.26.0**；插件 Web 路由在 `initialize()` 中通过 `context.register_web_api(...)` 注册。
2. 确认 **`views/dashboard/`** 目录完整（`index.html`/`app.js`/`style.css`）。AstrBot 4.29.4 的
   `PLUGIN_PAGE_ROOT_DIR_NAMES=("views","pages")` **优先 `views/`**；本仓库已删除 `pages/dashboard/`，
   不要再维护两份。
3. 前端调用契约：`window.AstrBotPluginView.apiGet/apiPost`（别名 `AstrBotPluginPage`），HTTP 路径为
   `/api/v1/plugins/extensions/astrbot_plugin_group_approval/...`；
   **旧 `/api/plug/...` 在 4.26+ 已不存在**，浏览器开发者工具里看到它即说明前端/宿主版本不匹配。
4. 若接口返回 `{"status":"error","retcode":401/403}`：这是插件自身的鉴权业务码（HTTP 仍可能为 200）。
   - 401：未登录/身份未被 AstrBot 识别；
   - 403：已认证但不是 Dashboard 管理员。
5. 修改静态资源后强制刷新（Ctrl+F5）。

---

## 3. 管理操作返回 401/403

- 本插件只承认 **AstrBot 唯一 Dashboard 账号**（`dashboard.username`）。只有 `request.username`
  与该账号**完全一致**时才是插件 OWNER。
- **QQ 的 Owner/Reviewer QQ 号不会赋予 Dashboard 权限**；两套身份不互通。
- `api_key:` 机器身份只能读，**不能**执行变更操作。
- 所有变更操作（approve/reject/grant/revoke/ban/unban/update_group）必须携带**纯数字且已核验**的
  `bot_self_id`；缺失/非数字/`"default"` 会返回 400 且不写库。

---

## 4. GroupGuardian 审核没有先执行（启动日志 `CONFLICT`）

### 现象
启动日志出现“内容审核优先级未满足安全要求 / CONFLICT”，或 `/api/.../diagnostics` 返回
`moderation_precedence: CONFLICT`。

### 原因
AstrBot 按 `handler.extras_configs["priority"]` 降序执行。上游 GroupGuardian 主审核 Handler 默认
`priority=0`，本插件闸门固定 `priority=50`，因此闸门先执行并 `stop_event()`，GroupGuardian 的审核
**不会运行**。

### 解决（必须）
在 GroupGuardian 插件目录内应用补丁：

```bash
git apply --check /path/to/astrbot_plugin_group_approval/patches/group_guardian_priority.patch
git apply         /path/to/astrbot_plugin_group_approval/patches/group_guardian_priority.patch
```

修订补丁使带 `priority=100` 的事件装饰器先注册。重启后检查 `VERIFIED`、主审核位置和每群配置。若顺序仍冲突，先暂停群消息接入，检查是否遗留早期补丁。 [补丁说明](../patches/README.md)

---

## 5. LivingMemory 仍记录了未授权用户的发言

LivingMemory 原版在 `WakingCheckStage` 启动采集，早于准入 Handler。关闭 `session_manager.enable_full_group_capture`，或应用兼容补丁后重载并在测试群检查落库。旧数据可能继续出现在总结和召回中，需要按部署的数据保留要求处理。 [补丁说明](../patches/README.md)

---

## 6. 用户达到群等级却提示“未达到申请门槛”

1. **核实 QQ 等级 vs 群成员等级**：插件只读群成员活跃等级（NapCat `level`），不读 QQ 账号等级（`qq_level`）。
2. **NapCat 缓存延迟**：申请/审批使用 `no_cache=True` 强制刷新；若腾讯结算延迟，稍后再试。
3. **群主关闭了群活跃等级展示**：腾讯会返回空/0，需在群设置中重新开启。

---

## 7. 提示“获取群成员信息失败（错误代码：10001）”

- 用户已退群/被踢，消息因网络延迟残留；
- NapCat 与腾讯断开或账号触发风控——查看 NapCat 日志；
- 若 Bot 身份无法核验（`get_login_info` 失败/多 Bot 歧义），插件会 **fail closed** 拒绝处理，这是预期行为。

---

## 8. 管理员收不到私聊审批提醒

1. 检查 `owner_qq_ids` / `reviewer_qq_ids` 是否填写正确（纯数字 QQ 号字符串）。
2. 检查 `notifications.reviewer_private_message` 是否为 `true`。
3. QQ 临时会话/好友风控可能拦截私聊；可在 `notifications.fallback_group_id` 配置**专用审核群**
   （管理员通知私聊失败后回退到该群；**不会**回退到申请人所在群）。
4. 查看 `notification_outbox` 表：`status`/`attempts`/`last_error`。队列为 **at-least-once**（重试可能重复发送，
   入队按稳定 `event_key` 去重）；达到 `max_retries` 后为 `FAILED`，当前没有自动死信接口，需要人工排查。

---

## 9. 申请人审批结果出现在了群里

- 默认情况下，审批结果（含备注/拒绝原因）**只私聊**发送给申请人，群内不会出现。
- 只有显式开启 `notifications.user_group_fallback_enabled=true` 后，私聊在重试上限后失败才向**申请来源群**
  发送 `user_group_fallback_text`（或内置最小文案）——该文案**不应包含**审批理由/备注。
- 若发现完整理由出现在群里，请检查是否有自定义代码/旧版本数据库字段（V6 之前的库需确认迁移已执行到 V6）。

---

## 10. 审计日志导出失败 / 下载的不是文件

- 导出接口 `audit_logs/export` 返回 `{status, retcode, format, filename, data}`；前端用真实 Blob 下载
  `audit_logs_<epoch>.json|csv`。
- `format` 仅支持 `json` / `csv`；其他值返回 400 错误（不会静默回退）。
- 导出内容受 `limit` 上限约束；大范围导出请缩小过滤条件。

---

## 11. 并发审批/撤销出现“该申请已被处理”或状态异常

- 审批在**单个 SQLite 事务**内完成（申请+授权+审计+通知入队），并校验 `rowcount`；并发审批同一申请时只有
  一个成功，其余返回明确“已被处理”提示。
- 授权状态转换使用 `revision` 乐观锁：并发撤销/封禁优先于自动恢复；若看到“状态已被其他操作变更”，
  重新读取当前状态即可，不会静默覆盖管理员操作。

---

## 12. 数据与迁移

- 数据库文件：AstrBot 数据目录 `data/plugin_data/astrbot_plugin_group_approval/group_approval.db`
  （以实际运行目录为准）。
- 迁移 V1–V6 均为增量、可重复执行：V2 建唯一索引前会先对旧 outbox 重复 `event_key` 去重；V4 增加
  `revision`；V5 增加 `reviewer_overrides`；V6 增加 `fallback_payload`/`claimed_at`；旧数据保留。
- 升级前建议备份该 `.db` 文件（含 `-wal`/`-shm`）。
