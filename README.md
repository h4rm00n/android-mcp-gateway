# Android MCP Gateway

把安卓系统能力变成 MCP 工具，供 AI 客户端（Claude / Cursor / 自研 Agent）调用。
设备主动出站连接自部署网关，穿 NAT、免公网 IP。

**单用户、自部署、只控制自己的手机。**

```
AI 客户端 (Claude / Cursor / 自研)
      │  MCP over Streamable HTTP  (HTTP + Bearer)
      ▼
┌────────────────────────┐        出站长连接（设备主动连出）
│ 自部署网关 (Python)     │◄──────────────────────────────┐
│ MCP 协议终止在这里      │                               │
└────────────────────────┘        ┌──────────────────────┴─┐
                                  │  Android App (Kotlin)   │
                                  │  前台服务 + 工具执行器    │
                                  └────────────────────────┘
```

设备端**不是** MCP server —— App 是 WS/TCP 客户端 + 工具执行器，MCP 协议终止在网关。

## 能力（MVP，无 root）

| 工具 | 实现 |
|---|---|
| `calendar_list_calendars` | `Calendars` 表 |
| `calendar_list_events` | **`Instances` 表**（循环事件按实例展开） |
| `calendar_create_event` / `update` / `delete` | `Events` + `Reminders`；支持循环事件、单实例删除（EXDATE） |
| `notification_read` / `dismiss` | `NotificationListenerService` |
| `notification_post` | Agent 自身名义发通知 |
| `device_status` | 电量、机型、在线状态 |
| `list_devices` / `list_paired_devices` / `unpair_device` / `generate_pairing_code` | 网关本地工具（设备发现、配对管理） |

**日历即闹钟**：用日历事件 + `Reminders.method` 实现闹钟语义。`METHOD_ALARM=4` 在小米日历上
是无视勿扰的真闹钟。method 原样透传不下调，交给日历 App 处理。

## 两种传输

| 传输 | 定位 | 说明 |
|---|---|---|
| **裸 TCP + Noise** | 主路径（App 默认） | 无应用层协议特征，可规避非标端口的 HTTP/WS 拦截。Noise_KK（X25519 + ChaChaPoly），与 WireGuard 同套件 |
| WSS | 调试/局域网 | 明文可抓包排查；或备案后走 443 + 反代 TLS |

两条路径**共用同一套消息协议与工具契约**（`session.py` 传输无关），切换只影响底层连接。

## 快速开始

```bash
# 网关
cd gateway
uv sync
cp config.example.json config.json
# 生成两个 token
openssl rand -hex 32   # → mcp.bearer
openssl rand -hex 32   # → pairing_token
uv run python -m gateway --config config.json

# 设备
cd ../app
gradle assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
# 授予权限
adb shell pm grant dev.amg.device android.permission.READ_CALENDAR
adb shell pm grant dev.amg.device android.permission.WRITE_CALENDAR
adb shell pm grant dev.amg.device android.permission.POST_NOTIFICATIONS
adb shell cmd notification allow_listener dev.amg.device/dev.amg.device.AgentNotificationListener
```

然后在 App 里填网关地址，让 AI 生成配对码完成配对。

**AI 客户端配置**（Claude / Cursor 等）：

```json
{
  "mcpServers": {
    "android": {
      "url": "https://gw.example.com/mcp",
      "headers": { "Authorization": "Bearer <mcp.bearer>" }
    }
  }
}
```

## 安全模型

| 层 | 机制 |
|---|---|
| 设备准入 | 统一配对凭证，或（TCP 路径）8 字符配对码 + PBKDF2 + HMAC 挑战应答，码永不上网 |
| 设备身份 | **X25519 静态公钥**（身份不可变）；设备名只是可变标签，改名不断连 |
| 会话加密 | Noise_KK 双向认证 + ChaChaPoly AEAD；或反代终结 TLS |
| MCP 客户端 | HTTP + Bearer token |
| 审计 | JSONL 全量记录（连接、调用、配对、改名、吊销），按大小轮转 |

配对记录可用 `list_paired_devices` 查看，`unpair_device` 吊销（在线会话立即断开）。

## 文档

| 文件 | 内容 |
|---|---|
| [AGENTS.md](AGENTS.md) | 项目决策总纲（架构、选型、踩过的坑） |
| [docs/protocol.md](docs/protocol.md) | WS/Noise 消息协议、认证、心跳、离线语义、超时 |
| [docs/raw-tcp-noise.md](docs/raw-tcp-noise.md) | 裸 TCP + Noise 传输设计、配对协议、验证记录 |
| [docs/deploy.md](docs/deploy.md) | 部署（nginx 终结 TLS、systemd、自检清单） |
| [gateway/README.md](gateway/README.md) | 网关运行与结构 |
| [app/README.md](app/README.md) | App 构建、联调、已知边界 |

## 已知边界

- **循环事件的单实例修改**未实现（删除已支持）。修改仍作用于整串。
- **设备私钥**无法进 Android Keystore 硬件区（纯 Java Noise 实现限制），用 Keystore 内 AES 密钥加密落盘。
- **保活**：Doze / 国产 ROM 后台查杀需要电池优化白名单 + 自启动白名单，属运维配置。
- `noiseprotocol` 停更在 0.3.1（密码学原语来自活跃维护的 `cryptography`）。
- 提醒的实际**触发**由日历 App 负责，非本项目的 Provider 写入逻辑。

## 状态

MVP 全部功能已完成并在 Android 13 实机（小米）+ Waydroid 验证：9 个设备工具全链路、
日历提醒触发（Etar 秒级）、循环事件展开与单实例删除、双设备并存、改名/吊销/重配对。

**裸 TCP + Noise 已在公网实测通过**（国内云服务器非标端口）——当初引入该路径的原始动机
（规避非标端口上的 HTTP/WS 协议特征拦截）已成立。设备端网络恢复即重连；网关吊销设备时回
`DEVICE_REVOKED` 拒绝帧，App 停机等人工重新配对而不是无限重试。
