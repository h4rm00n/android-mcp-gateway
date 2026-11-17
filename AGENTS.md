# Android MCP Gateway

把安卓系统能力转换为 MCP 服务：Android 设备侧 Agent 主动出站连接到自部署网关注册工具，网关对外统一提供 MCP（Streamable HTTP），供 AI 客户端（Claude / Cursor / 自研 Agent）调用。**单用户、自部署、只控制自己的手机。**

## 架构

```
AI 客户端 (Claude/Cursor/自研)
      │ MCP over Streamable HTTP
      ▼
┌────────────────┐     WSS 出站长连接（设备主动出站，穿 NAT，免公网 IP）
│ 自部署网关       │◄───────────────────────┐
│ TypeScript      │                        │
└────────────────┘     ┌──────────────────┴─┐
                       │ Android Agent App   │
                       │ 能力层 + 前台服务保活   │
                       └────────────────────┘
```

- **网关**：Python 3.12+（uv 管理）+ 官方 python-sdk（`mcp`）+ starlette/uvicorn + websockets。单服务即可，同一端口服务 `/mcp`（Streamable HTTP）与 `/device/ws`。职责：静态工具目录的 `tools/list`、路由 `tools/call`（请求 ID 关联 WS 会话，等结果回传）、鉴权、审计。实现见 `gateway/`。
- **设备**：Kotlin App。前台服务保持 WSS 长连接（心跳 + 指数退避重连）；连接后 hello 断言 `toolset_version`（不上报 manifest）；"设备离线"作为一等状态暴露给 AI（不傻等超时）。
- 设备端**不是** MCP server——App 是 WS 客户端 + 工具执行器，MCP 协议终止在网关。

## 技术选型（已定稿）

**Python + uv**，否决记录（避免将来重议）：

- **否决 Rust**：网关负载是个位数 QPS 的 JSON 转发（延迟大头在手机 Provider 与网络 RTT），性能优势无从兑现；rmcp 成熟度折价；迭代慢。合法场景是做成多人分发产品（单二进制价值大）——不是本项目目标。
- **否决 Node/TS**：官方 TS SDK 是 MCP 参考实现（最大让渡项），但 Python SDK 对本项目特性子集（tools/list、call 路由、Streamable HTTP）覆盖完整、差距小；且用户明确排斥 node 部署生态。uv 使部署接近单命令（`uv sync --frozen`，自动管理解释器版本）。
- 协议层语言无关（docs/protocol.md），将来换语言重写网关范围有界。

## 工具目录静态化（关键决策）

- 工具目录（名称/描述/inputSchema/timeout_ms）**静态定义在网关代码**（`gateway/gateway/catalog.py`），`tools/list` 无条件返回完整目录，与设备在线状态解耦——离线不闪断，调用直接报 `DEVICE_OFFLINE`。
- 网关不实现 `tools/list_changed`；设备 hello 不上报 manifest，只断言 `toolset_version`（两侧各持同一常量，任一侧改 schema/语义须同时 bump，防两部署面漂移）。
- args 校验只在设备端做（设备持同一份 schema），网关透传——单一权威。
- **工具分两类**（2026-11-17）：
  - **设备工具**（9 个）：目录在 `catalog.py`，强制带 `device` 参数，转发前剥离。
  - **网关本地工具**（`list_devices`）：在网关内直接回答，**不需要 `device`**，用于让 AI 发现"有哪些设备、谁在线"，避免猜名字。它不影响设备的 `TOOLSET_VERSION`（设备不实现它）。
- `list_devices` 返回在线设备 + **有历史记录的离线设备**（含 model/android/last_online/offline_for_s），因此 AI 能区分"从没连过的名字"与"连过但现在离线"。历史存在内存（网关重启清空）。
- ~~动态聚合（hello 上报 manifest）留给二期多设备~~ **多设备已落地（2026-11-17，用户决策）**：MCP 层每个工具强制携带 `device` 参数（自由字符串=设备自报的名字，**不带 enum**——设备名单动态），网关按它路由并**转发前剥离**——设备契约/schema/TOOLSET_VERSION 完全不受多设备影响，App 零改动。名字离线/不存在 → `DEVICE_OFFLINE` 并**附当前在线名单**（供 AI 自纠）。未选方案：默认设备路由、`mi14__tool` 命名空间（名字翻倍）。

## MVP 范围（已定稿）

无 root、纯标准 Android API、侧载安装：

| 工具 | 实现方式 |
|---|---|
| `calendar_list_calendars` | `Calendars` 表：`_id` / `calendarDisplayName` / `isPrimary` / `calendarAccessLevel`(≥500 可写) / `color` / `accountName` |
| `calendar_list_events` | **必须查 `Instances` 表**（按时间范围展开循环事件），不要直接查 Events（rrule 不会展开） |
| `calendar_create_event` / `calendar_update_event` / `calendar_delete_event` | `Events` + `Reminders` 表；运行时权限 `READ_/WRITE_CALENDAR` |
| `notification_read` / `notification_dismiss` | `NotificationListenerService`（用户在设置中授予"通知使用权"） |
| `notification_post` | 以 Agent 自身名义 `notify()`（普通权限） |
| `device_status` | 电量、在线状态等，让 AI 能感知设备状态 |

**明确不做（MVP）**：系统闹钟 App 读写（二期，需 root）、UI 自动化（三期，无障碍服务）、伪造其他 App 通知（价值低）、RemoteInput 通知直接回复（二期）、Magisk 模块/root 通道（二期，MVP 完全不需要）、多设备复杂治理（二期，但协议须预留 device_id）。

### 日历即闹钟（关键决策）

用户场景："某时某地参加某活动，提前 N 分钟用某方法通知"。用日历事件 + Reminders 实现闹钟语义，不做系统闹钟 App：

- `Reminders.method` **直接透传，不做校验降级，交给日历软件自行处理**。`METHOD_ALERT=1` 通用；`METHOD_ALARM=4` 传给日历 App——小米日历的 ALARM 是真闹钟、无视勿扰（已确认，这是目标设备）；`METHOD_EMAIL=2` 仅 Google 账户；`METHOD_SMS=3` 基本已死。
- 可选能力探测：读目标日历 `Calendars.allowedReminders` 位掩码（bit0=ALERT, bit1=EMAIL, bit2=SMS）。
- 真正发通知的是管理该日历账户的日历 App，不是 Provider——需确认目标手机装有已启用的日历 App。

### 日历 Provider 字段要点（API 14+，稳定不变）

- 插入 Event 硬性要求：`calendar_id` + `dtstart` + `dtend` 或 `duration` 二选一（**循环事件必须 `duration`，不能 `dtend`**）。
- **`DURATION` 只对循环事件有效**（2026-11-17 实机验证，Android 13 + MIUI）：非循环事件若只写 `DURATION` 不写 `DTEND`，Provider 不做换算，回读时 `DTEND` 为空/等于 `DTSTART`，**事件时长为 0**。非循环事件必须在工具层把 `duration` 换算成 `DTEND = DTSTART + 毫秒`。循环事件才写 `DURATION`。
- **Update 时 DTEND 与 DURATION 互斥**：Provider 拒绝同一事件同时含两者，报 `Cannot have both DTEND and DURATION in an event`。改写时长时必须对另一种 `putNull`。
- **Instances 只在 DTSTART 参与 update 时才重算**（实机验证）：仅改 `DTEND`/`DURATION` 时 Instances 保留旧值，`calendar_list_events` 读回旧时长。故任何时间变更都必须把 `DTSTART` 一并写回（值不变也写），强制重建实例。
- **更新循环事件必须同时回写 `RRULE`**（实机验证）：只写 `DTSTART` 而不写 `RRULE` 时，Provider 会把整串退化成单实例且时长归零。正确组合是 `DTSTART + RRULE + 变更字段`。
- **单实例删除用 `EXDATE`**（实机验证）：`DTSTART + RRULE + EXDATE` 三件套一起 update 才能既排除该次实例又保住其余实例；EXDATE 为逗号分隔的 UTC 时间列表（`yyyyMMdd'T'HHmmss'Z'`），可累积多次删除。`calendar_delete_event` 的 `occurrence_start` 参数由此实现；不传则删整串。
- 全天事件：`dtstart` 用 UTC 零点 + `duration=P1D` + `allDay=1` + `eventTimezone=UTC`，否则跨时区偏移一天。此转换在工具层做，不暴露给 AI。
- 提醒在 `Reminders` 表独立插入（Event 插入后拿回 `event_id`），一条提醒一行，多条提醒插多行。
- AI 入参时间用 ISO 8601，Agent 内部转 epoch 毫秒 + 时区。
- 循环事件单实例修改需 `original_id` + `originalInstanceTime` 例外记录（二期）。**单实例删除已于 2026-11-17 落地**（EXDATE 路线，见上）。

### `calendar_create_event` 参数面（AI 视角）

```
title (string, 必填)
description (string, 可选)
location (string, 可选)
start_time (ISO 8601, 必填)
end_time (ISO 8601, 与 duration 二选一)
duration (RFC 5545 duration, 与 end_time 二选一)
all_day (bool, 默认 false)
timezone (string, 默认设备时区)
recurrence (RFC 5545 RRULE 透传, 可选)
reminders (int[] 提前分钟数, 可选，含 method 时扩展为对象数组)
calendar_id (long, 可选，缺省 isPrimary=1 主日历)
availability (BUSY/FREE/TENTATIVE, 可选)
```

## 传输安全（已定稿，细节见 docs/protocol.md；部署见 docs/deploy.md）

- **TLS 归属：交给 nginx 终结**（2026-11-17 定稿，符合"所有 HTTP 服务原样部署、证书统一交 nginx"的习惯）。网关只提供明文 HTTP/WS，**不碰证书代码**；App 侧也**不做传输策略限制**（明文全放行、无 IP 白名单）——安全边界归网关与反代，APK 只管连接。`listen.cert/key` 仅保留给"不经反代、网关自己终结 TLS"的可选路径。WebSocket 反代与普通 HTTP 相同（多两条 upgrade 头 + 关闭 `proxy_read_timeout`）。
- **反代部署的两个必配项**（否则是隐藏炸弹）：
  - `listen.trusted_proxies`（如 `["127.0.0.1"]`）：仅对受信反代采信 `X-Forwarded-For`，还原真实客户端 IP。**不配则所有设备在限速器眼里都是 127.0.0.1——一台被攻击，全员被封**，且审计日志失去溯源。已实现 `netutil.client_ip`（取最右侧非受信 hop，防左侧伪造），6 个单测覆盖。
  - `mcp.allowed_hosts`：DNS rebinding 防护（Host 头白名单），实测非白名单 Host 返回 421。
- **启动期危险组合拦截**：明文 + 监听 0.0.0.0 + 未声明 trusted_proxies → 打印明文暴露警告并给出两种正确姿势；明文 + 回环、或已声明反代 → 仅 info。
- **设备↔网关**：WSS 必须（TLS 保密 + token 验准入，两层不可互替）。
- **token = 统一配对凭证**（frp 模型）：网关配置单一 `pairing_token`，只决定"能不能连"，**不承载身份**。
- **设备身份 = hello 自报 `device_id`**（2-32 非空白字符，区分大小写；App 输入框默认填机型名）。**在线唯一**：同名已在线的 → `error(NAME_IN_USE)` + close 4000，App 置 fatal 等人工改名（防两台同名无限互踢）。
- 常数时间比较；**同 IP 连续失败 5 次 → 封 10 分钟**——弱 token 场景的主防线；连接/调用全审计。
- **token 允许手工设置弱值**（用户决策，单用户自部署取舍）：生成建议 `openssl rand -hex 32`，启动时 <16 字符仅告警不阻止。
- **已论证并否决**：裸 TCP（要自造分帧/心跳，丢掉与 MCP 服务共享 443）、SSH 协议（独立端口 + 登录面 foot-gun + 无法用 Android Keystore 硬件密钥）、TOFU 互传公钥（首连即 MITM 窗口）、**per-device token 映射反查**（隔离更强但一机一码运维负担重，改为统一凭证 + 自报名）、**自研"取公钥/发公钥 + 明文 WS 传密文"**（等于重写 TLS：中继攻击、重放、AEAD/前向保密全部要自己防；公钥预置就是 pinning，应直接用 TLS 而非自造加密层）。
- **残余风险（如实声明，已接受）**：① 持 token 者可登记任何未被在线会话占用的名字，包括冒用**离线**设备的名字（在线时会被 NAME_IN_USE 挡住）；② 服务器被攻破（pinning 也挡不住，防线在运维最小化）；③ 配对 token 从设备侧泄露（二期 mTLS 客户端证书绑定硬件密钥才是根治）。**网络路径 MITM 由 TLS（nginx 终结）覆盖；若后续要防域名劫持/CA 误签，在 App 加 OkHttp CertificatePinner 钉 SPKI 指纹，是独立增强项，与反代架构不冲突。**
- **MCP 侧**：HTTPS + 独立 Bearer 令牌，成熟方案，无需额外设计。

## 传输双路径（2026-11-17）

| 路径 | 优先级 | 状态 | 用途 |
|---|---|---|---|
| **裸 TCP + Noise** | **主路径**（App 默认，UI 在前） | ✅ Waydroid + 真机全链路跑通 | 规避国内云厂商对非标端口 HTTP/WS 的协议特征拦截（实测换端口无效——DPI 按特征识别，不看端口） |
| WSS | 次路径（App UI 标"调试"） | ✅ 生产可用 | 局域网调试/抓包排查（Noise 是黑盒，WS 明文可读）；或备案完成后走 443 + 反代 TLS |

**保留 WS 的理由**：成本仅约 100 行（`device_ws.py` 45 + `WsLink` 56），因为消息循环已抽到
传输无关的 `session.py`（448 行两者共用），不存在双份业务逻辑漂移；而它提供明文可观测性
（Noise 通道抓包是密文）、局域网零配对调试、以及备案后升级到标准 TLS 的能力。

裸 TCP 路径要点（细节见 `docs/raw-tcp-noise.md`）：

- **密码学**：Noise Protocol Framework，`Noise_KK_25519_ChaChaPoly_SHA256`（与 WireGuard 同套件）。
  Python `noiseprotocol` 0.3.1 ↔ Java `com.github.auties00:noise-java:1.2` **已实测互通**。
  **不自研密码学**——之前否决的"取公钥/明文传密文"方案等于重写 TLS。
- **配对**：AI 调 `generate_pairing_code` 生成 8 字符 Crockford Base32 码（40 bit），人眼读取填入手机。
  **码只作 HMAC 密钥，永不上网**（proof = HMAC(PBKDF2(码,salt), role‖nonce‖设备名‖双方公钥)）。
  PBKDF2 300k + TTL 5min + 单次有效 + 失败限速（防 CPU DoS）。双向 proof 提供 MITM 检测。
- **身份**：配对即绑定 name↔静态公钥，后续连接走 KK（双向认证）。设备先发 `SHA256(名字)[:8]` 定位公钥。
- **复用**：`session.py` 抽出传输无关的消息循环，WS 与 TCP 共用 hello/heartbeat/tool_call/离线语义，
  工具契约与 `TOOLSET_VERSION` 完全不变。
- **设备私钥**：X25519 无法进 Keystore 硬件区（所有纯 Java Noise 实现都如此），用 Keystore 内 AES 密钥加密落盘。

## 下一步（新会话从这里开始）

1. ~~写协议文档~~ ✅ `docs/protocol.md` v1。
2. ~~落定 MVP 全部工具的 JSON Schema~~ ✅ 静态目录在 `gateway/gateway/catalog.py`（9 工具，`TOOLSET_VERSION=1`，设备 App `AppConfig.kt` 持同一常量）。
3. ~~两端开发~~ ✅ **全部完成并端到端验收通过**（2026-11-17，Waydroid x86_64 / Android 13）：
   - 网关：`gateway/`（uv 项目，实现对照 protocol.md 全项，pytest 9 个含端到端全绿）。
   - App：`app/`（构建/安装/授权/联调步骤见 app/README.md；ABI 无关通用 APK）。
   - 验收链路实测通过：MCP 客户端 → 网关 → WSS → Waydroid → 真实日历 Provider/通知系统，9 个工具全链路 ✓；`reminders.method=ALARM(4)` 透传入库确认。
4. ~~验收场景~~ ✅ **2026-11-17 全部实测通过**：验收事件（提前 1 天 + 30 分钟、method=ALARM）入库并查回确认；**提醒实际触发亦已验证**——Waydroid 上装有 Etar（org.lineageos.etar，LineageOS 日历，支持通知提醒、不支持闹钟），插入 3 分钟后的事件 + 提前 1 分钟 ALERT，Etar 在 start-60s **秒级精确**发出系统通知并被 `notification_read` 读回（插入即调度，无需重启日历 App）。**ALARM(method=4) 真闹钟路径留给真机小米日历**（Etar 不支持；这正是 method 透传不降级设计的预期行为）。可选加固：App 接 ConnectivityManager 网络恢复即连、真机保活（Doze/自启动白名单）。

实现注意（踩过的坑，改代码前先看）：

- **mcp SDK 是 2.x**：低层 API 为构造器注册（`Server(name, on_list_tools=..., on_call_tool=...)`），无装饰器；类型字段 snake_case（`input_schema`/`is_error`/`structured_content`），线上序列化自动转 camelCase；客户端为 `streamable_http_client`（返回二元组 streams，自定义 header 走 `httpx2.AsyncClient`）。
- **starlette 1.x 路由**：函数端点一律按 `func(request)` 处理，ASGI 必须传类实例；`Mount` 会对无尾斜杠路径 307 重定向——MCP 端点用 `Route` + 鉴权写在端点内。
- 2.x 取消模型不再 task-cancel handler：低层 handler 拿不到取消事件，故 MVP 不转发 `tool_cancel`（协议已标预留）。
- **Android 端坑**：`network_security_config` **全局放行明文、不做 IP 白名单**（用户决策：安全交给网关/反代，APK 不参与传输策略；且安卓 `<domain>` 规则不支持 CIDR/通配，白名单会绑死网段——实测换同网段另一 IP 即报 `CLEARTEXT not permitted`）。App 地址**无内置默认值**，界面填 `ws://ip:port/device/ws` 或 `wss://...`；`Instances.CONTENT_URI` 已含 `/when`；全新系统（Waydroid）无日历账户，App 端自举 LOCAL 日历兜底；`am start-foreground-service` 拉不起未导出服务，走 Activity 自动启动。

## 二期 / 三期备忘

- **二期**：多设备注册与命名空间、完整鉴权（证书 pinning / mTLS 客户端证书、扫码配对、多 MCP 客户端细粒度鉴权）、审计后台、通知 RemoteInput 直接回复（无 root 可行，按 App 适配）。
- **二期 Magisk 模块路线**（解锁真·闹钟）：`service.sh` 开机拉起 root daemon，本地 socket + `SO_PEERCRED` 校验对端 uid，命令白名单（只允许 `content` / `cmd notification` / `dumpsys`）；闹钟读写用 `su -c content --uri content://com.android.deskclock/alarms` 走时钟 App 自己的 Provider（AMS 对 uid=0 放行非导出组件，App 内部逻辑会正确重排 AlarmManager 调度）。**不要直接改 SQLite 文件**——时钟 App 内存有缓存，改文件不生效。MVP 后若目标设备是小米，其时钟 App Provider 结构需单独逆向适配。
- **三期**：无障碍服务 UI 自动化（优先返回控件树而非截图，省 token）、设备端插件化能力注册、OEM 时钟 App adapter。
- **保活坑（Android 端最大工程问题）**：Doze / 后台查杀 → 前台服务 + 常驻通知必须，电池优化白名单引导，国产 ROM 自启动白名单；断线指数退避重连。
- **安全基线**（即使自部署也要做）：设备→网关设备令牌、客户端→网关鉴权、全部调用审计日志。敏感操作分级授权从第一天进架构。
