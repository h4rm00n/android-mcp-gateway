# Android 设备端（Kotlin App）

WS 客户端 + 工具执行器。MCP 协议终止在网关，本 App 只说 `docs/protocol.md` 的 WS 协议。

## 构建

```bash
cd app
/tmp/tools/gradle-8.9/bin/gradle assembleDebug   # 或系统 gradle >= 8.9
# 产物: app/build/outputs/apk/debug/app-debug.apk（ABI 无关，x86_64/arm 通用）
```

SDK 路径在 `local.properties`（`sdk.dir`）。AGP 8.7.3 + Kotlin 2.0.21，`minSdk=29 / targetSdk=34`。

## Waydroid 本机联调

```bash
adb connect 192.168.240.112:5555     # IP 见 waydroid status
adb install -r app/build/outputs/apk/debug/app-debug.apk
adb shell pm grant dev.amg.device android.permission.READ_CALENDAR
adb shell pm grant dev.amg.device android.permission.WRITE_CALENDAR
adb shell pm grant dev.amg.device android.permission.POST_NOTIFICATIONS
adb shell cmd notification allow_listener dev.amg.device/dev.amg.device.AgentNotificationListener
adb shell am start -n dev.amg.device/.MainActivity   # 打开即自动连网关
```

宿主机跑网关：`cd ../gateway && uv run python -m gateway --config config.waydroid-dev.json`
（监听 0.0.0.0:18437；App 默认连 `ws://192.168.240.1:18437/device/ws`）。
联调脚本：`cd ../gateway && uv run python scripts/e2e_waydroid.py`。

## 关键实现点

| 文件 | 内容 |
|---|---|
| `GatewayService.kt` | 前台服务（dataSync 类型）+ AgentConnection：hello/心跳（2 次无 ack 重连）/指数退避 1s→60s±30%/TOOLSET_MISMATCH 停机防死循环 |
| `CalendarTools.kt` | 日历×5。Instances 表展开循环事件；循环事件强制 duration；全天事件 UTC 零点+P1D+eventTimezone=UTC 转换在此层；reminders.method 原样透传；**无日历时自举 LOCAL 日历**（全新系统兜底） |
| `NotificationTools.kt` | read/dismiss 走 NotificationListenerService（adb 可授予）；post 走自身 NotificationManager。正文按 **BIG_TEXT → EXTRA_MESSAGES（MessagingStyle 聊天消息）→ TEXT_LINES（Inbox）→ TEXT → ticker** 回退提取；附 `app` 显示名（需 Manifest 里的 `<queries>` 包可见性声明）、`sub_text`、`category`。支持 `include_ongoing` / `non_empty_only` 过滤空壳通知 |
| `AppConfig.kt` | **`TOOLSET_VERSION` 必须与网关 `catalog.py` 同步 bump**；`deviceName`（默认机型名，空白转 `-`）即上报给网关的身份 |
| `network_security_config.xml` | 仅放行 Waydroid 桥接 IP/模拟器地址的明文 WS，生产 WSS 不受影响 |

坑位记录：`Instances.CONTENT_URI` 已含 `/when`（勿重复 append）；`Calendars` 颜色列是 `CALENDAR_COLOR`；`requestRebind()` 需显式 ComponentName；`adb shell cmd notification allow_listener` 在 MIUI 上可能"显示已授权但实际未绑定"——`disallow_listener` 再 `allow_listener` 可强制重绑。

**时长语义（实机验证，2026-11-17）**：非循环事件只写 `DURATION` 不写 `DTEND` → Provider 不换算，时长变 0；
`App` 已改为在工具层把 `duration` 换算成 `DTEND`（循环事件才用 `DURATION`）。Update 时 DTEND/DURATION
互斥必须 `putNull`，且**必须同时写回 `DTSTART`** 才能让 Instances 表重算（否则回读旧时长）。
回归脚本：`cd ../gateway && uv run python scripts/test_duration.py`、`scripts/test_recurring.py`。

**循环事件（实机验证，2026-11-17）**：更新循环事件必须同时回写 `RRULE`（只写 `DTSTART` 会把整串退化成单实例）；
单实例删除走 `EXDATE`（`DTSTART + RRULE + EXDATE` 三件套一起 update）。`calendar_delete_event` 的
`occurrence_start` 参数即此实现，不传则删整串。

## 传输方式（App 内可切换）

| 方式 | 何时用 | 前置 |
|---|---|---|
| **WS (wss)** | 常规部署，网关前有反代终结 TLS | 填 `wss://host:port/device/ws` + `pairing_token` |
| **裸 TCP (Noise)** | 国内云厂商拦截非标端口 HTTP/WS 时 | 填网关 IP/端口 → **配对**（让 AI 生成配对码）→ 启动 |

裸 TCP 配对流程：

1. 对 AI 说"给 mi14 生成配对码"，AI 调用 `generate_pairing_code` 得到如 `K7M2-P9XR`
2. App 里选「裸 TCP (Noise)」，填主机/端口，填入配对码，点「配对」
3. 看到「✓ 配对成功（网关公钥 …）」即完成；点「保存并启动」
4. 之后启动不再需要配对码（已落盘 name↔公钥）

配对成功后设备私钥用 Keystore 内 AES 密钥加密落盘（X25519 无法直接进 Keystore 硬件区）。

## 配置（App 界面三项，无内置默认值）

| 项 | 说明 | 示例 |
|---|---|---|
| 网关地址 | `ws://` 或 `wss://` + 主机 + 端口 + `/device/ws` | `ws://192.168.1.10:18437/device/ws`<br>`wss://gw.example.com/device/ws` |
| 配对 token | 网关配置里的 `pairing_token`（所有设备共用一个） | `openssl rand -hex 32` 生成的串 |
| 设备名称 | 自报身份，在线唯一，默认取机型名 | `mi14` |

地址无默认值——首次打开需自行填写（输入框有格式提示）。`ws://` 与 `wss://` 均支持：
TLS 由网关前面的反代终结，**App 不参与任何证书/传输策略判断**（`network_security_config`
已全局放行明文，不做 IP 白名单——安卓的 domain 规则不支持 CIDR/通配，白名单反而会绑死网段）。

身份模型：token 是**统一配对凭证**（网关 `pairing_token`，所有设备共用一个），设备身份由界面里
"设备名称"自报，**在线唯一**。两台设备填同一个名字 → 后连的收到 `NAME_IN_USE` 并置 fatal
（不自动重试），需改名后重新"保存并启动"。

## 已知边界（MVP）

- 闹钟实际**触发**由管理日历的日历 App 负责。Waydroid 内的 Etar（org.lineageos.etar）已实测：ALERT 提醒插入即调度、start-60s 秒级触发通知 ✓；ALARM(method=4) Etar 不支持——按设计不降级、透传给日历 App，真闹钟路径在真机小米日历验收。
- 重连退避已实现；网络恢复回调（ConnectivityManager）未接，靠退避兜底。
- `tool_cancel` 协议预留，网关不转发，设备按 timeout_ms 自终止。
