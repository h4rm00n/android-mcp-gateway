# 裸 TCP + Noise 传输（实验分支 `experiment/raw-tcp-noise`）

> **状态（2026-11-17）：已完成并在 Waydroid 上全链路跑通。**
> 网关 48 个测试全绿；设备经 Noise 通道跑通全部工具（含 duration / 循环事件回归）；
> 审计日志确认 `transport: "tcp+noise"`。

## 为什么要这条路

腾讯云等厂商对非标端口的 **HTTP/WS 流量按协议特征拦截**（不看端口号）：
明文 HTTP 的 `GET /`、TLS 的 SNI、WS 的 `Upgrade` 头都是可识别特征。实测换端口无效。

能活下来的传输必须**没有已知应用层特征**。裸 TCP + Noise 加密载荷满足这一点：
首次字节即随机，无可匹配的协议指纹。

代价：TLS 提供的「保密、认证、抗重放、前向保密」要自己承担——本方案用 **Noise Protocol
Framework** 而不是自研密码学，密码学部分交给成熟实现。

## 已验证的可行性（spike）

| 项 | 结果 |
|---|---|
| Python `noiseprotocol` 0.3.1 | KK / XX / IK / XXpsk3 均可构造 |
| Java `noise-java` 1.2（`com.southernstorm`） | HandshakeState / CipherState 可用 |
| **Python ↔ Java 互通** | ✅ 实测握手成功 + 双向加密消息往返（`spike/noise_interop/`） |

这是本方案最高技术风险点，已排除。`noise-java` 是 Android 兼容的纯 Java 实现
（被 Signal 等使用过），无需 JNI。

## 传输层

```
TCP
 └─ 帧：[u32 BE 长度][载荷]         ← 载荷 = Noise 密文
     └─ Noise_KK 会话（长连接，一条 TCP = 一个设备会话）
         └─ 明文 = JSON envelope（复用 protocol.md §3 的消息层，原样不变）
```

**关键复用**：加密通道内跑的仍是 `protocol.md` 定义的消息（hello / heartbeat /
tool_call / tool_result / error）。传输改造不影响消息语义、工具契约、`TOOLSET_VERSION`。

Noise 单条消息明文上限 65535 字节，因此传输层做分块：每块前置 1 字节标志
（`0`=还有后续，`1`=末块），接收端累积到末块再解析。单条消息上限 256 KB（与 WSS 版一致）。

## 身份与配对

### 身份 = 公钥，名字 = 标签（2026-11-17 修正）

早期实现用**设备名**派生定位 tag（`SHA256(name)[:8]`），导致设备一改名就断连、且 keystore
里留下陈旧条目。现改为：

| 概念 | 是什么 | 可变性 |
|---|---|---|
| **身份** | 设备 X25519 静态公钥 | **不可变**，配对时绑定 |
| **设备名** | 用户填写的标签 | 可变，随时改 |
| **定位 tag** | `SHA256(公钥)[:8]` | 随公钥，与名字无关 |

行为：

- 设备改名后连接**照常成功**（tag 不变），功名随之更新；审计记 `device_renamed`
- 改名撞到另一台已配对设备的名字 → 拒绝（`NAME_IN_USE`），防冒名顶替
- 同一公钥重新配对 → **迁移标签**而非新增条目（不留垃圾记录）
- 名字在线唯一仍在 `session.py` 校验（两条传输一致）

配对记录维护工具（网关本地工具，不需要 `device` 参数）：

| 工具 | 用途 |
|---|---|
| `list_paired_devices` | 列出已配对设备：名字、公钥指纹、配对时间、改名时间、是否在线 |
| `unpair_device` | 吊销设备（删公钥）；若在线会**立即断开**，之后无法再连接 |

### 静态密钥

- **网关**：一对 X25519 静态密钥，持久化在 `gateway.keys`（首次启动生成）。
- **设备**：一对 X25519 静态密钥，存 App 私有存储（用 Android Keystore 内的 AES 密钥
  加密后落盘——**X25519 私钥无法直接放进 Keystore**，所有纯 Java Noise 实现都有此限制）。
- 配对完成后双方各自持有对方的静态公钥，后续连接走 **Noise_KK**（双方静态密钥互相已知，
  提供双向身份认证 + 前向保密）。

### 首次配对：带外配对码 + HMAC 挑战应答

配对码由 AI 通过 MCP 工具生成（`generate_pairing_code`），人眼读取后填入 App。
**配对码作为 HMAC 密钥使用，永不出现在网络报文中**。

```
device → gw:  {mode:"enroll", device_name, device_pub}
gw → device:  {nonce (32B random), gateway_pub}
device → gw:  {proof_dev = HMAC(K, "dev" ‖ nonce ‖ device_name ‖ device_pub ‖ gateway_pub)}
gw  校验 proof_dev → 失败则断开（不透露原因）
gw → device:  {proof_gw  = HMAC(K, "gw"  ‖ nonce ‖ device_name ‖ device_pub ‖ gateway_pub)}
device 校验 proof_gw → 失败说明被中间人替换，断开
```

`K = HMAC-SHA256(配对码, "amg-pair-v1")`。

**安全性论证**：

| 攻击 | 为什么失败 |
|---|---|
| 窃听 | proof 不含码；64-bit 码无法离线穷举（2^64 HMAC 计算） |
| 冒充网关（中间人） | 攻击者不知 K，无法生成 proof_gw；且 gateway_pub 被绑定进两条 proof，替换公钥必然验签失败 |
| 冒充设备 | 无法生成 proof_dev |
| 重放 | nonce 每次新生成；proof 绑定 nonce + 双方公钥 |
| 配对码泄露 | TTL 5 分钟 + 单次有效；用完即作废 |

**注意**：配对码会出现在 AI 上下文里（经过 AI 服务商），所以「只有你和手机知道」不成立。
靠 64-bit 熵 + 短 TTL + 单次使用把风险压到可接受。

### 配对码格式

64 bit CSPRNG，base32 编码为 13 字符，分组显示：`K7M2-P9XR-4WTB-N`。
避免 `0/O/I/1` 等易混字符。生成即写审计。

## 连接建立流程

```
① 设备已配对？
   是 → mode = session
   否 → mode = enroll

② 帧 0（1 字节 mode）：
   0x01 = enroll   → 走 HMAC 挑战应答（明文 JSON 帧）
   0x02 = session  → 走 Noise_KK 握手

③ session 模式下，设备先发 8 字节 device tag：
   tag = SHA256(device_name)[:8]     ← 网关据此查设备的静态公钥
   （只暴露哈希前缀，不泄露设备名）
   网关若查不到 tag → 断开（未配对设备不能建会话）

④ Noise_KK 两轮握手完成 → 进入加密通道

⑤ 加密通道内首帧必须是 hello（协议 §4），之后走原有消息循环
```

## 网关侧改动

| 模块 | 改动 |
|---|---|
| `noise_wire.py` | 新增：分帧、Noise_KK 会话、配对协议 |
| `pairing.py` | 新增：配对码生成/校验（TTL、单次） |
| `keystore.py` | 新增：网关静态密钥 + 已配对设备表（name→pubkey）持久化 |
| 消息循环 | 从 `device_ws.py` 抽出为通用 `Channel` 接口，WS 与 Noise 共用 |
| `catalog.py` | 新增网关本地工具 `generate_pairing_code`（不影响 TOOLSET_VERSION） |
| 配置 | `listen.transport: "wss" \| "noise"`，两者可同时开（迁移期） |

## 已知风险与取舍

| 项 | 说明 |
|---|---|
| `noiseprotocol` 维护状态 | 0.3.1（2019 年后无新版本）。密码学原语来自 `cryptography`（活跃维护），Noise 状态机部分稳定但不再更新。替代方案：`noiseprotocol` 不可用时自实现 KK（~200 行，RFC 明确）或换用 `snow`（Rust，需 pynacl/ffi 桥接） |
| 设备私钥保护 | 无法进 Keystore 硬件区，只能软件加密落盘。设备被 root 后可提取 → 需吊销机制（二期：网关侧删设备公钥） |
| DPI 抗性 | 无应用层特征，但「长期单 TCP 连接 + 周期性小包」仍可能被统计特征识别。真被针对需流量整形或换端口/中转（超出本方案范围） |
| 无标准审计工具 | `ss`/`tcpdump` 只能看到加密载荷；排障依赖网关自身日志 |

## 验证记录

### 已通过

1. ✅ Python ↔ Java KK 握手 + 双向加密（`spike/noise_interop/`）
2. ✅ 分帧单测：含 >120KB 消息跨块传输（`NOISE_CHUNK` 明文上限 65535，按明文分块逐块加密）
3. ✅ 配对：成功 / 错码 / 无码 / TTL 过期 / 单次作废 / 已用码重放
4. ✅ **中间人检测**：攻击者替换 gateway 公钥 → 设备的 proof 绑定错误公钥 → 网关验签失败
5. ✅ 未配对设备建会话 → 直接断开（tag 查不到）
6. ✅ 已配对设备冒充他人名字 → hello 校验拒绝
7. ✅ 篡改密文 → AEAD 完整性失败 → 断开
8. ✅ 配对失败限速（5 次/10min，防 PBKDF2 CPU DoS）
9. ✅ **MCP 全链路**：generate_pairing_code → 配对 → KK 会话 → tool_call → tool_result
10. ✅ **Waydroid 实机**：配对成功 → 会话建立 → 全部工具通过（e2e / duration / recurring）

### 待验证（需要公网）

- 腾讯云非标端口是否放行裸 TCP（这是本分支的**原始动机**，必须在目标环境实测）
- 长时间连接稳定性（运营商 NAT 空闲回收、移动网络切换）

## 公网实测步骤

```bash
# 1) 服务器上启动（tcp.enabled=true，端口选高位，如 40000-50000 之间）
uv run python -m gateway --config config.json

# 2) 服务器本地确认监听
ss -ltn | grep 18438

# 3) 从外部网络（手机流量）测端口可达性 —— 关键一步
#    用手机热点让笔记本走蜂窝网络，然后：
nc -zv <服务器IP> 18438        # 应显示 succeeded / open
#    若超时 → 云安全组没开；若被 RST → 可能被 DPI 拦（本方案失效）

# 4) 手机 App 填网关 IP/端口 → 让 AI 生成配对码 → 配对 → 启动服务
# 5) 审计日志确认 transport
grep tcp+noise audit.jsonl | tail -1
```

**判断标准**：第 3 步连得上、第 5 步有 `tcp+noise` 记录、工具调用正常。
若第 3 步超时，先查安全组/防火墙，再考虑 DPI。

## 与 WSS 路径的关系

- `master` 分支的 WSS 实现**保持不变**，仍可用（AI 客户端侧本来就是 HTTP，不受封锁影响）。
- 两条路径共用：工具目录、消息协议、注册表、审计、限流。
- 设备端根据配置选择 transport（App 里可切换），便于对比和回退。

## 验证计划

1. ✅ Python ↔ Java KK 握手 + 双向加密（已通过，`spike/noise_interop/`）
2. 单测：分帧（含 >64KB 分块）、配对成功/错码/TTL 过期/重放、MITM 检测
3. 端到端：Waydroid App（Noise 客户端）↔ 网关（Noise 服务端）跑通 10 个工具
4. 公网实测：腾讯云非标端口，确认不被拦
