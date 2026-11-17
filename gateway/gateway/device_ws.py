"""设备侧 WS 端点 /device/ws（薄壳：认证后交给通用会话逻辑）。"""

from __future__ import annotations

import hmac

from starlette.websockets import WebSocket

from .audit import AuditLog
from .channel import WsChannel
from .config import GatewayConfig
from .netutil import client_ip
from .ratelimit import AuthRateLimiter
from .registry import DeviceRegistry
from .session import run_device_session


def make_device_ws_endpoint(
    cfg: GatewayConfig,
    registry: DeviceRegistry,
    limiter: AuthRateLimiter,
    audit: AuditLog,
):
    async def endpoint(websocket: WebSocket) -> None:
        ip = client_ip(websocket, cfg)

        # 认证（upgrade 阶段）：统一配对凭证，只管准入不管身份
        auth_header = websocket.headers.get("authorization", "")
        token_bytes = (auth_header[7:] if auth_header.startswith("Bearer ") else "").encode("utf-8")
        token_ok = hmac.compare_digest(token_bytes, cfg.pairing_token.encode("utf-8"))
        if limiter.banned(ip) or not token_ok:
            limiter.record_fail(ip)
            await audit.log("auth_fail", ip=ip)
            try:
                await websocket.close(code=1008)  # 握手前拒绝 → HTTP 403
            except Exception:  # noqa: BLE001
                pass
            return
        limiter.record_success(ip)
        await audit.log("auth_ok", ip=ip)

        await websocket.accept()
        await run_device_session(WsChannel(websocket), registry, audit, ip, expected_device_id=None)

    return endpoint
