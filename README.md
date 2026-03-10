# SynoChat Print Bot 📱→🖨️

在手机上通过 Synology Chat 机器人发送图片或 PDF，家里的打印机自动打印。

---

## 功能一览

- 📎 支持 PDF、JPG、PNG、TIFF、BMP
- 📄 PDF 自动识别页数并在预览中显示
- 🔑 确认暗号防止误触发打印
- 🖨️ 打印完成后自动通知
- ⏰ 超时未确认自动取消并通知
- ⚙️ 支持双面/横纵向等打印选项，默认黑白打印

---

## 架构

```
手机
 └─ SynoChat 机器人（发送图片/PDF）
       └─ Webhook
             └─ 本程序（运行在树莓派上）
                   └─ CUPS → 打印机 🖨️
```

---

## 部署步骤

### 1. 环境准备（树莓派）

```bash
sudo apt update
sudo apt install cups python3 python3-requests -y
sudo usermod -aG lpadmin $USER
```

查看打印机名称：
```bash
lpstat -p -d
```

### 2. 部署程序文件

```bash
sudo mkdir -p /opt/synochat-print-bot
sudo chown $USER:$USER /opt/synochat-print-bot
cp server.py config.json /opt/synochat-print-bot/
```

### 3. 填写 config.json

```json
{
  "synochat_token": "机器人的 Token",
  "synochat_incoming_url": "https://NAS地址:端口/webapi/entry.cgi?api=SYNO.Chat.External&method=incoming&version=2&token=xxx",
  "synas_base_url": "https://192.168.1.100:5001",
  "synas_user": "NAS专用账号",
  "synas_pass": "密码",
  "printer_name": "7080D",
  "bot_user_id": "7",
  "listen_host": "0.0.0.0",
  "listen_port": 8765,
  "max_file_mb": 50,
  "confirm_timeout_sec": 300
}
```

> `bot_user_id`：SynoChat 机器人在 NAS 上的用户 ID，用于定位附件存储路径。
> 可在 File Station → chat → @ChatWorking → uploads 目录下查看，找到与机器人对应的子文件夹编号。

### 4. 配置 SynoChat

#### 4a. 创建机器人（接收消息用）

1. SynoChat → 右上角头像 → **整合** → **机器人** → **新增**
2. 填写名称（如 `打印机器人`）
3. Webhook URL 填：`http://树莓派内网IP:8765/webhook`
4. 保存后复制生成的 **Token** → 填入 `config.json` 的 `synochat_token`

#### 4b. 创建 Incoming Webhook（Bot 回复消息用）

1. SynoChat → 整合 → **传入的 Webhook** → 选择频道 → **生成令牌**
2. 复制完整 URL → 填入 `config.json` 的 `synochat_incoming_url`
3. 注意 URL 中 token 参数不要包含多余的引号

#### 4c. 给 NAS 专用账号开放权限

1. 控制面板 → 共享文件夹 → `chat` → 编辑 → 权限
2. 给专用账号添加**读取**权限

### 5. 设置开机自启

```bash
# 编辑 service 文件，把 User=YOUR_USERNAME 改为实际用户名（通常是 pi）
nano synochat-print-bot.service

sudo cp synochat-print-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now synochat-print-bot
sudo systemctl status synochat-print-bot
```

---

## 使用方法

### 发送文件

直接向机器人发送图片或 PDF。Bot 会回复打印预览：

```
📋 打印预览
文件：report.pdf
信息：PDF · 12 页 · 340 KB
选项：份数 1 ｜ 纵向 ｜ 单面 ｜ 黑白

🔑 确认打印，请回复暗号：慵懒的熊猫
🚫 回复「取消」放弃 ｜ ⏰ 5 分钟内有效
```

回复暗号确认后：
```
🖨️ 已发送到「7080D」，打印完成后会通知您～
```

打印完成时：
```
✅ 打印完成！文件已全部输出。
```

### 打印选项

可在发送文件时同时输入选项文字（通过相册分享等方式在消息框附带文字）。

**简码**（位1·位2）：

| 简码 | 含义 |
|------|------|
| `11` | 单面·纵向（默认） |
| `12` | 单面·横向 |
| `21` | 双面翻长边·纵向 |
| `22` | 双面翻长边·横向 |
| `31` | 双面翻短边·纵向 |
| `32` | 双面翻短边·横向 |

**份数**：`x3` 或 `3份`

**色彩**：默认黑白，需要彩色加「彩色」

**自然语言**也支持：`双面 横向 x2 彩色`

### 常用指令

| 发送内容 | 效果 |
|----------|------|
| `帮助` | 显示使用说明 |
| `取消` | 取消当前待打印任务 |

---

## 通知说明

| 通知 | 触发时机 |
|------|----------|
| ⬇️ 正在接收... | 收到文件后立即 |
| 📋 打印预览 + 暗号 | 文件下载完成后 |
| 🖨️ 已发送到打印机 | 暗号确认后 |
| ✅ 打印完成 | CUPS 任务队列清空时 |
| ⏰ 等待超时，已取消 | 5 分钟内未回复暗号 |
| ⚠️ 打印超时 | 打印任务超过 5 分钟未完成 |

---

## 日常维护

```bash
# 查看实时日志
sudo journalctl -u synochat-print-bot -f

# 重启服务（修改配置后执行）
sudo systemctl restart synochat-print-bot

# 查看打印队列
lpq

# 取消所有打印任务
cancel -a
```

---

## 排错

| 问题 | 解决方案 |
|------|---------|
| Bot 不回复 | 检查 `synochat_incoming_url` 格式，token 不含多余引号 |
| 文件接收失败 | 检查 NAS 账号权限，`chat` 共享文件夹需要读取权限 |
| 不支持的格式 | 确认发送的是 PDF/JPG/PNG/TIFF/BMP |
| 打印失败 | 运行 `lpstat -p` 确认打印机在线；检查纸张/墨水 |
| Webhook 收不到 | 检查防火墙是否放行 8765 端口 |

---

## 安全建议

- 使用专用 NAS 账号，只开放 File Station 和 chat 目录读取权限
- 程序只监听局域网，不要将 8765 端口暴露到公网
- Token 验证已内置，防止非法调用
