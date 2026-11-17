# 网关 ↔ 设备 WS 协议 v1

状态：MVP 定稿。`proto_version: 1`。

## 1. 总览

- **传输**：WSS（TLS 必须，明文 WS 禁止）。**TLS 由 nginx 等反代终结**（部署见 `docs/deploy.md`）——网关自身只监听明文回环/内网，不实现证书逻辑；也支持网关自行终结 TLS（`listen.cert/key`）。
- **设备是唯一发起方**（出站连接），网关只监听。
- **认证**：HTTP upgrade 阶段 Bearer token（frp 模型），详见 §2。
- **消息**：JSON 文本帧，统一 envelope 封装，详见 §3。
- **MCP 终止在网关**：设备不感知 MCP。本协议的 `tool_call` / `tool_result` 与 MCP 的 `tools/call` 一一对应，由网关翻译。
- 单用户自部署。多设备治理、mTLS、pinning 见 §12 与 AGENTS.md 二期备忘。

## 2. 连接与认证

端点：`wss://<host>/device/ws`

```
GET /device/ws HTTP/1.1
Host: gateway.example.com
Upgrade: websocket
Authorization: Bearer <pairing_token>
```

- **token 是统一配对凭证**（frp 模型）：只决定"能不能连上网关"，不承载身份。网关配置单一 `pairing_token`。
- **设备身份 = hello 里自报的 `device_id`**（§6）：2-32 个非空白字符，区分大小写。App 界面提供输入框（默认取机型名）。
- **唯一性只约束在线设备**：报名字时若已有同名在线会话 → `error(NAME_IN_USE)` + close 4000；设备不得用同名自动重试（App 置 fatal，等人工改名），否则两台同名设备会无限互踢。
- token 比较常数时间；**失败限速**：同 IP 连续失败 5 次 → 封禁 10 分钟（弱 token 场景的主防线）。
- 审计：每次 `auth_ok` / `auth_fail` / `online`（含 device_id）/ `offline` 记 `{ts, ip, device_id?, reason}`。
- **信任边界（如实声明）**：持 token 者可登记任何未被在线会话占用的名字，包括冒用离线设备的名字——单用户自部署接受该残余风险；收紧路径是二期 mTLS（客户端证书绑定身份，upgrade 阶段换血，消息协议不变）。

## 3. 消息封装

所有帧必须是 JSON **文本帧**（二进制帧 → `PROTOCOL_ERROR`）。

```json
{"type": "hello", "id": "h-1", "ts": 1765700000000, "payload": {}}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `type` | 是 | 消息类型，见 §4 |
| `id` | 请求类必填 | 应答回带同一 id |
| `ts` | 否 | 发送方本地 epoch ms，仅诊断，不依赖时钟同步 |
| `payload` | 是 | type 专属内容；接收方忽略未知字段（前向兼容） |

上限：单帧 256 KB。

## 4. 消息类型

| type | 方向 | 触发 | payload |
|---|---|---|---|
| `hello` | D→G | 连接建立后首帧 | §6 设备信息 + `toolset_version` |
| `hello_ack` | G→D | 校验通过 | `session_id`, `heartbeat_interval_ms`, `server_time` |
| `heartbeat` | D→G | 每 interval | `{}` |
| `heartbeat_ack` | G→D | 收到 heartbeat | `{}` |
| `tool_call` | G→D | MCP `tools/call` | `tool`, `args`, `timeout_ms` |
| `tool_result` | D→G | 执行完成 | §7 |
| `tool_cancel` | G→D | （预留：MVP 网关不转发取消） | `call_id` |
| `error` | 双向 | 协议级错误 | `code`, `message` |

时序规则：

- 连接后**首帧必须是 `hello`**（10 秒窗口），否则 `PROTOCOL_ERROR` + close 4000。
- `tool_result.id` 必须对应本会话未决的 `tool_call`；未知 id → 记 error 后丢弃。
- 设备按接收顺序**串行**执行 `tool_call`（并发固定为 1；多并发留二期再入协议）。

错误码（`error.code`）：

| code | 含义 | 谁产生 |
|---|---|---|
| `PROTOCOL_ERROR` | 封装/时序违规 | 双方 |
| `TOOLSET_MISMATCH` | 设备 `toolset_version` 与网关工具目录不一致 | 网关 |
| `TOOL_NOT_FOUND` | 未知工具名 | 设备 |
| `ARGS_INVALID` | 入参不符合 inputSchema | 设备 |
| `PERMISSION_DENIED` | 运行时权限未授予（如日历/通知使用权） | 设备 |
| `DEVICE_INTERNAL` | 设备端执行异常 | 设备 |
| `CANCELLED` | 收到 `tool_cancel` 且尚未执行完 | 设备 |
| `NAME_IN_USE` | 设备名已被在线会话占用 | 网关 |
| `TIMEOUT` | 超时（网关侧计时，也可能设备内更短的超时先触发） | 双方 |
| `DEVICE_OFFLINE` | 设备离线（仅网关→MCP 侧，不出现在 WS 上） | 网关 |

## 5. 心跳与离线判定

- `hello_ack.heartbeat_interval_ms` 下发间隔，默认 30000。
- 设备每 interval 发 `heartbeat`，网关立即回 `heartbeat_ack`。
- **任何来自对方的帧都重置静默计时器**（心跳不是唯一活性来源，`tool_result` 也算）。
- 网关侧：设备静默 ≥ 3×interval → close 4003，标记离线。
- 设备侧：连续 2 次 heartbeat 未收到 ack → 主动断开，进入重连。

离线语义（“设备离线是一等状态”的落地）：

- 网关对该设备所有未决 `tool_call` **立即**以 `DEVICE_OFFLINE` 失败，不等超时。
- 工具**保留**在 MCP `tools/list` 中（清单稳定，不闪断），调用即刻报错。
- 设备侧 `device_status` 工具返回在线/离线状态，供 AI 主动查询。
- 工具目录静态（见 §6），网关无需实现 `notifications/tools/list_changed`。

## 6. 工具目录静态 + hello 兼容性断言

**工具目录（名称/描述/inputSchema/timeout）静态定义在网关**，不随设备连接变化：MCP `tools/list` 始终返回完整固定目录，无设备在线时照常返回（调用按 §5 立即报 `DEVICE_OFFLINE`）。网关不实现 `tools/list_changed`。

目录（网关侧）与执行实现（App 侧）是两个独立部署面——网关重启 vs App 侧载，节奏不同，漂移风险用版本断言钉住。hello 因此不携带完整 manifest，只带：

```json
{
  "proto_version": 1,
  "device_id": "mi14",
  "toolset_version": 3,
  "device": {
    "model": "MI 14",
    "android": 34,
    "app_version": "0.1.0",
    "capabilities": ["calendar", "notifications"]
  }
}
```

- `device_id`：自报设备名，2-32 个非空白字符，区分大小写；在线唯一（§2）。App 默认取机型名（空白转 `-`）。

- `toolset_version`：工具契约版本号（int）。网关目录与设备 App 各持同一常量，与目录定义写在网关代码同一处管理；任一侧修改任何工具的 schema/语义，必须两侧同时 bump。
- 校验：`proto_version` major 不一致，或 `toolset_version` 不一致 → `error(TOOLSET_MISMATCH)` + close 4000；设备不得原样重连重发（防死循环）。
- **多设备路由（MCP 层约定，不进设备契约）**：每个 MCP 工具的入参强制携带 `device`（string，即自报的设备名）；网关按它选择路由目标，**转发前剥离该参数**——设备收到的 `tool_call.args` 永远不含 `device`，故设备 schema 与 `toolset_version` 均不受多设备影响。`device` 缺失 → `ARGS_INVALID`；名字离线或不存在 → `DEVICE_OFFLINE`（错误信息附当前在线名单，供 AI 自纠）。
- 设备名单是**动态的**（自报登记制），因此 `device` 参数不带 enum；AI 从用户/上下文获知设备名，或从 `DEVICE_OFFLINE` 错误的在线名单中自纠。
- **args 校验只在设备端做**（设备持有同一份 schema），网关透传不校验——单一权威，避免双重校验漂移。
- 二期多设备时重新评估：届时目录可能按设备配置化，或恢复 hello 携带 manifest 的动态聚合模式；MVP 单设备下动态聚合是零收益的复杂度。

## 7. tool_call / tool_result / 超时

```
G→D  {"type":"tool_call","id":"c-9f3a","payload":{"tool":"calendar_create_event","args":{...},"timeout_ms":30000}}

D→G  {"type":"tool_result","id":"c-9f3a","payload":{"ok":true,"result":{"event_id":4821}}}
D→G  {"type":"tool_result","id":"c-9f3a","payload":{"ok":false,"error":{"code":"PERMISSION_DENIED","message":"READ_CALENDAR 未授予"}}}
```

- `call_id` 由网关生成，会话内唯一。
- **超时**：网关按 `timeout_ms` 计时；到时即向 MCP 客户端返回 `TIMEOUT`，迟到的 `tool_result` 丢弃并记审计。
- `result` 为任意 JSON；网关翻译为 MCP 工具结果：`content[0].text` = JSON 序列化，`structuredContent` = result 原文。
- `tool_cancel`（预留）：MVP 网关不转发 MCP 客户端取消——设备一律按 `timeout_ms` 自行终止，无泄漏窗口；消息类型保留，二期若做转发，设备以 `error(CANCELLED)` 收尾。

## 8. 重连

- 指数退避：1s 起，×2，上限 60s，±30% 抖动；连接稳定 60s 后退避重置。
- 网络恢复事件（Android `registerNetworkCallback`）→ 立即重连。
- 每次连接完整重跑 auth + hello；**同 device_id 新连接顶替旧连接**（旧连接由网关 close 4001），用于清理半开僵尸连接。

## 9. WebSocket close code

| code | 含义 |
|---|---|
| 4000 | 协议错误 / 版本或 toolset 不匹配 |
| 4001 | 被同 device_id 新连接顶替 |
| 4003 | 心跳静默超时 |
| 4004 | 网关主动下线（维护/重启），可在 error 帧携带 `retry_after_s` |

设备对任何 close 都按 §8 重连；收到 4004 时按 `retry_after_s` 延迟。

## 10. 网关配置（示例）

```json
{
  "listen": {
    "host": "127.0.0.1",
    "port": 18437,
    "cert": null,
    "key": null,
    "trusted_proxies": ["127.0.0.1", "::1"]
  },
  "mcp": {
    "path": "/mcp",
    "bearer": "<MCP 客户端令牌>",
    "allowed_hosts": ["gw.example.com"]
  },
  "pairing_token": "<设备配对凭证，统一一个>"
}
```

- **TLS 由反代终结**（推荐）：`host` 设回环，`trusted_proxies` 声明反代地址以还原真实客户端 IP（不配则限速器把所有设备当成同一个 IP）。`cert`/`key` 仅在网关自己终结 TLS 时填。
- `mcp.allowed_hosts`：Host 头白名单（DNS rebinding 防护），留空关闭。
- 设备侧只有一个 `pairing_token`（配对凭证）；身份由设备自报 `device_id` + 在线唯一性约束（§2）。
- MCP 客户端鉴权独立（MVP 单 Bearer 令牌；多客户端/细粒度鉴权见二期）。
- 同一端口同时服务 `/mcp`（Streamable HTTP，AI 客户端）与 `/device/ws`（设备）。

## 11. 审计日志（JSONL）

```json
{"ts":1765700000000,"event":"call","device_id":"mi14","call_id":"c-9f3a","tool":"calendar_create_event","ip":"1.2.3.4"}
```

`event` ∈ `auth_ok | auth_fail | online | offline | call | result | timeout | cancel`

## 12. 明确不做（协议层，防过度设计）

- 消息压缩、分片、二进制编码——量级极小，JSON 够用。
- 设备侧主动推送（新通知事件上报）——MVP 只拉取，推送语义二期再议。
- 会话恢复/断点续传——重连即完整重注册，状态全量重建。
- 端到端加密叠加——TLS + token 已覆盖自部署威胁模型（伪造网关的剩余场景：域名过期抢注、CA 误签，接受该残余风险，二期上证书 pinning / mTLS，升级不改动本消息协议）。
- 动态工具目录与 `tools/list_changed` 通知——目录静态定义在网关（设计决策），设备 hello 只断言 `toolset_version`；动态聚合留给二期多设备。
