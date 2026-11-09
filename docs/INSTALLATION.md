# 安装与部署指南 (INSTALLATION.md)

本指南针对 **Windows 10/11** 操作系统及 **AstrBot Launcher** 部署环境编写。

---

## 1. 环境准备

- **操作系统**：Windows 10 / 11 64位
- **Python 版本**：Python 3.12+
- **协议端**：[NapCatQQ](https://github.com/NapNeko/NapCatQQ) 或基于 OneBot v11 标准协议的反向 WebSocket 端
- **主程序**：AstrBot v4.16.0+（由 AstrBot Launcher 启动管理）

---

## 2. 安装方式

### 方式一：AstrBot Launcher / WebUI 扩展商店安装（推荐）

1. 打开 AstrBot Launcher 或登录 AstrBot Web 控制台。
2. 进入导航栏 **扩展 -> 插件**。
3. 点击右上角 **从 GitHub 仓库安装**。
4. 输入仓库链接：
   ```text
   https://github.com/Maxwell233/astrbot_plugin_group_approval
   ```
5. 点击 **确定安装**，AstrBot 将自动拉取源码并安装依赖（`aiosqlite`）。
6. 安装完成后，重启 AstrBot 实例或点击 **热重载**。

---

### 方式二：手动克隆至插件目录

1. 定位 AstrBot 数据目录中的插件文件夹：
   ```powershell
   cd <AstrBot根目录>\data\plugins
   ```
2. 执行 Git 克隆：
   ```powershell
   git clone https://github.com/Maxwell233/astrbot_plugin_group_approval.git astrbot_plugin_group_approval
   ```
3. 为 AstrBot Python 环境安装依赖：
   ```powershell
   <AstrBot环境路径>\python.exe -m pip install -r astrbot_plugin_group_approval\requirements.txt
   ```
4. 在 AstrBot Web 控制台重新加载插件。

---

## 3. 初始管理员绑定

首次启动时，必须在配置文件中指定最高管理员（Owner）的 QQ 号：

1. 打开 AstrBot WebUI -> 扩展 -> 插件 -> 点击 **群等级准入与管理员审批** 卡片。
2. 在可视化配置页面中找到 **Owner QQ 列表**（`owner_qq_ids`），填入最高管理员的 QQ 号（例如 `123456789`）。
3. 保存配置，插件将立即对该管理员开放 `/ga` 核心指令集。

---

## 4. 验证运行状态

在 QQ 群内或向 Bot 发送私聊消息测试：
```text
/ga status
```
若正常返回类似如下内容，说明插件已成功加载并工作：
```text
【Group Approval 插件运行状态】
• 插件状态：运行中 (Enabled)
• 受管启用群数：1
• 待审核申请数：0
• 已授权活跃用户：0
• 暂停授权用户：0
• 黑名单人数：0
• Owner 人数：1
• 审核员人数：0
```
