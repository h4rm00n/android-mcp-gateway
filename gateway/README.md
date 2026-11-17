# Android MCP Gateway（网关端）

把安卓设备工具聚合为 MCP（Streamable HTTP）。协议见 [docs/protocol.md](../docs/protocol.md)。

## 运行

```bash
cd gateway
uv sync                        # 自动创建 venv、按锁安装（含解释器）
cp config.example.json config.json
# 编辑 config.json：mcp.bearer 与 pairing_token 用 openssl rand -hex 32 生成

uv run python -m gateway --config config.json
```

生产部署见 [docs/deploy.md](../docs/deploy.md)：**TLS 由 nginx 终结**，网关监听回环明文。
反代部署别忘了配 `listen.trusted_proxies`（还原真实 IP，否则限速器全站共享一个 IP 桶）。
`listen.cert/key` 只在不用反代、网关自己终结 TLS 时才需要。
设备端连接 `wss://<host>/device/ws`，AI 客户端连接 `https://<host>/mcp`。

## 接入 AI 客户端（Claude / Cursor 等）

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

工具目录是**静态**的（`gateway/catalog.py`）——设备离线时列表不变，调用即刻返回
`DEVICE_OFFLINE`（附当前在线设备名单）。改目录必须与设备 App 的 `TOOLSET_VERSION` 同步 bump。

**多设备**：设备用统一 `pairing_token` 连入，在 hello 里自报设备名（App 界面可改，默认机型名）。
每个工具调用必须带 `device` 参数点名目标设备。同名在线冲突 → `NAME_IN_USE`，设备需改名重连。

## 结构

| 文件 | 职责 |
|---|---|
| `catalog.py` | 静态工具目录：9 个工具的名称/描述/JSON Schema/timeout |
| `config.py` | 配置加载与弱 token 告警 |
| `device_ws.py` | `/device/ws`：Bearer 认证、失败限速、hello 校验、心跳看门狗、会话顶替 |
| `mcp_app.py` | MCP 服务：`tools/list` + `tools/call` 路由（超时/取消/离线语义） |
| `registry.py` | 设备会话与在途调用 Future |
| `ratelimit.py` | 同 IP 连续失败 5 次 → 封 10 分钟 |
| `audit.py` | 审计日志（JSONL） |

## 测试

```bash
uv run pytest
```
