# 部署（nginx 终结 TLS）

**架构决策**：TLS 统一由 nginx 处理，网关只提供明文 HTTP/WS，监听回环或内网。
这与"所有 HTTP 服务原样部署、证书交给 nginx"的习惯一致，网关代码不碰证书。

```
AI 客户端 ──https/wss──> nginx（443，证书在此）──http/ws──> gateway（127.0.0.1:18437）
Android 设备 ──wss──> ────────┘
```

WebSocket 走 nginx 与普通 HTTP 反代**完全一样**——多两条 upgrade 头即可，无需额外模块。

## 一、网关配置

```json
{
  "listen": {
    "host": "127.0.0.1",
    "port": 18437,
    "trusted_proxies": ["127.0.0.1", "::1"]
  },
  "mcp": {
    "path": "/mcp",
    "bearer": "<openssl rand -hex 32>",
    "allowed_hosts": ["gw.example.com"]
  },
  "pairing_token": "<openssl rand -hex 32>",
  "audit_log": "/var/log/amg/audit.jsonl"
}
```

三个关键点：

| 配置 | 作用 | 不配的后果 |
|---|---|---|
| `host: 127.0.0.1` | 只监听回环，公网只能经反代进来 | 若监听 0.0.0.0，18437 端口成为明文旁路，绕开 TLS |
| `trusted_proxies` | 采信来自反代的 `X-Forwarded-For` | 所有设备在限速器眼里都是 127.0.0.1：**一台被攻击 → 全员封禁**；审计日志失去溯源能力 |
| `allowed_hosts` | DNS rebinding 防护（校验 Host 头） | 恶意网页可借浏览器对 127.0.0.1 发起请求 | 

## 二、nginx 配置

```nginx
# WebSocket upgrade 需要把 Connection 透传
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen 443 ssl;
    http2 on;
    server_name gw.example.com;

    ssl_certificate     /etc/letsencrypt/live/gw.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/gw.example.com/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;

    # 设备长连接端点
    location /device/ws {
        proxy_pass http://127.0.0.1:18437;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # 关键：设备长连接必须关掉读超时，否则 60s 空闲就被 nginx 掐断
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        proxy_buffering off;
    }

    # MCP 端点（AI 客户端）
    location /mcp {
        proxy_pass http://127.0.0.1:18437;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;          # MCP 有流式响应
        proxy_read_timeout 300s;
    }
}
```

**不做**的两件事，以及为什么：

- **不按 nginx 的做法把 `listen.cert` 填上**——网关侧不需要证书。`listen.cert/key` 只在"不想用反代、网关自己终结 TLS"时才用。
- **不用 nginx 做设备认证**——`Authorization` 头原样透传给网关，认证逻辑只有一处（网关），避免两处规则漂移。

## 三、设备端

App 里填 `wss://gw.example.com/device/ws`，配对 token 填网关 `pairing_token`。
反代部署下**不需要**在 APK 里做证书 pinning 才能工作（nginx 用正常 CA 证书，系统信任链即可）。

> 若后续想钉死证书（防域名劫持/CA 误签），做法是在 App 里加 OkHttp `CertificatePinner`，
> 钉 `gw.example.com` 的 SPKI 指纹。这是独立增强项，与反代架构不冲突。

## 四、systemd

`/etc/systemd/system/amg-gateway.service`：

```ini
[Unit]
Description=Android MCP Gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=amg
Group=amg
WorkingDirectory=/opt/amg/gateway
ExecStart=/opt/amg/.local/bin/uv run --frozen python -m gateway --config /opt/amg/config.json
Restart=always
RestartSec=3

# 最小权限
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/log/amg
CapabilityBoundingSet=
AmbientCapabilities=

[Install]
WantedBy=multi-user.target
```

首次部署：`uv sync --frozen`（在 `/opt/amg/gateway` 下），`mkdir -p /var/log/amg && chown amg:amg /var/log/amg`。

## 五、部署后自检清单

```bash
# 1) 网关只监听回环（不应出现 0.0.0.0:18437）
ss -ltnp | grep 18437

# 2) 明文端口从外部不可达（应超时/拒绝）
curl -m 3 http://<服务器公网IP>:18437/mcp   # 必须失败

# 3) 经反代走 HTTPS 正常（401 = 通了但没 token，符合预期）
curl -o /dev/null -w '%{http_code}\n' https://gw.example.com/mcp

# 4) WS 握手经反代正常（101 或 401，不是 400/426）
curl -i -m 5 -H "Connection: Upgrade" -H "Upgrade: websocket" \
     -H "Sec-WebSocket-Version: 13" -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
     -H "Authorization: Bearer <pairing_token>" \
     https://gw.example.com/device/ws | head -3

# 5) 设备连上后确认审计里是真实 IP（不是 127.0.0.1）
tail -3 /var/log/amg/audit.jsonl

# 6) 设备上执行一次 list_devices + device_status
cd /opt/amg/gateway && AMG_MCP_URL=https://gw.example.com/mcp \
  AMG_MCP_TOKEN=<mcp.bearer> uv run python scripts/e2e_waydroid.py <设备名>
```

第 5 条最容易翻车：如果审计里全是 `127.0.0.1`，说明 `trusted_proxies` 没配对——**限速器此时是全站共享的**，运维上会变成隐藏炸弹。

## 六、为什么这个项目不考虑证书代码

| 关注点 | 归属 |
|---|---|
| 证书申请、续期（certbot）、OCSP、TLS 版本与套件 | nginx |
| 反代后的真实 IP、Host 校验 | 网关（`trusted_proxies` / `allowed_hosts`） |
| 设备/客户端身份 | 网关（pairing_token / mcp.bearer） |
| 应用层加密 | 不做——TLS 已覆盖 |

把证书交给 nginx 不是"省事"，而是**把证书生命周期放在已经有工具链和监控的地方**。网关只需要正确认识"我在反代后面"这件事。
