"""真实客户端 IP 解析（反代部署必需）。

nginx 终结 TLS 时，网关看到的对端永远是 127.0.0.1。若不还原真实 IP：
- 认证失败限速会把「所有人」当成同一个 IP —— 一台设备被攻击，全员被封；
- 审计日志里全是 127.0.0.1，事后无法定位。

因此仅在**对端是受信反代**时才采信 `X-Forwarded-For`（取最右侧非受信项），
防伪造：未受信来源带 XFF 头一律忽略。
"""

from __future__ import annotations

from starlette.websockets import WebSocket

from .config import GatewayConfig


def client_ip(websocket: WebSocket, cfg: GatewayConfig) -> str:
    peer = websocket.client.host if websocket.client else "?"
    if not cfg.listen.behind_proxy or not cfg.listen.is_trusted_proxy(peer):
        return peer

    raw = websocket.headers.get("x-forwarded-for", "")
    if not raw:
        return peer
    # XFF: client, proxy1, proxy2 ... —— 从右往左跳过受信反代，第一个非受信即真实客户端
    hops = [h.strip() for h in raw.split(",") if h.strip()]
    for hop in reversed(hops):
        if not cfg.listen.is_trusted_proxy(hop):
            return hop
    return hops[0] if hops else peer
