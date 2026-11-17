"""装配：Starlette app（/device/ws + /mcp 同端口）+ uvicorn 启动。"""

from __future__ import annotations

import argparse
import contextlib
import hmac
import sys

import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.types import ASGIApp, Receive, Scope, Send

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings

from .audit import AuditLog
from .config import GatewayConfig, load_config
from .device_ws import make_device_ws_endpoint
from .mcp_app import build_mcp_server
from .ratelimit import AuthRateLimiter
from .registry import DeviceRegistry


def _bearer_authorized(scope: Scope, token: bytes) -> bool:
    headers = dict(scope.get("headers", []))
    auth = headers.get(b"authorization", b"")
    return auth.startswith(b"Bearer ") and hmac.compare_digest(auth[7:], token)


class McpEndpoint:
    """ASGI 端点（starlette 1.x：类实例才按 ASGI 处理）。

    Bearer 鉴权 + Streamable HTTP。用 Route 而非 Mount：避免对 /mcp → /mcp/ 的 307。
    """

    def __init__(self, session_manager: StreamableHTTPSessionManager, token: str) -> None:
        self._session_manager = session_manager
        self._token = token.encode("utf-8")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not _bearer_authorized(scope, self._token):
            resp = JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await resp(scope, receive, send)
            return
        await self._session_manager.handle_request(scope, receive, send)


def create_app(cfg: GatewayConfig) -> Starlette:
    registry = DeviceRegistry()
    limiter = AuthRateLimiter()
    audit = AuditLog(cfg.audit_log, cfg.audit_max_bytes, cfg.audit_backups)

    mcp_server = build_mcp_server(cfg, registry, audit)
    # stateless + json_response：单用户场景的最小形态，无 SSE 流、无会话粘滞
    # allowed_hosts：DNS rebinding 防护（反代场景填对外域名/IP；留空则关闭该校验）
    security = None
    if cfg.mcp.allowed_hosts:
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(cfg.mcp.allowed_hosts),
        )
    session_manager = StreamableHTTPSessionManager(
        app=mcp_server, json_response=True, stateless=True, security_settings=security
    )

    device_ws = make_device_ws_endpoint(cfg, registry, limiter, audit)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        async with session_manager.run():
            yield

    return Starlette(
        lifespan=lifespan,
        routes=[
            Route(cfg.mcp.path, McpEndpoint(session_manager, cfg.mcp.bearer),
                  methods=["GET", "POST", "DELETE"]),
            WebSocketRoute("/device/ws", device_ws),
        ],
    )


def _warn_about_tls(cfg: GatewayConfig) -> None:
    """TLS 归属检查：网关自身明文时，必须确认它在反代后面或只监听回环。

    决策（AGENTS.md）：TLS 统一交给 nginx 终结，网关只提供明文 HTTP。
    因此这里的职责不是"逼你配证书"，而是**拦住危险的默认组合**：
    明文 + 监听 0.0.0.0 + 未声明受信反代 = 公网明文，必须显式知情。
    """
    loopback = cfg.listen.host in ("127.0.0.1", "localhost", "::1")

    if cfg.listen.tls_enabled:
        return

    if cfg.listen.behind_proxy:
        print(
            f"info: 明文监听 {cfg.listen.host}:{cfg.listen.port}，"
            f"信任反代 {', '.join(cfg.listen.trusted_proxies)}（X-Forwarded-For 生效）。"
            "请确认反代已终结 TLS 且该端口未直接暴露公网。",
            file=sys.stderr,
        )
        return

    if loopback:
        print(
            f"info: 明文监听回环 {cfg.listen.host}:{cfg.listen.port}——仅本机可访问，"
            "预期由同机反代转发。",
            file=sys.stderr,
        )
        return

    print(
        f"warning: 明文监听 {cfg.listen.host}:{cfg.listen.port} 且未声明 listen.trusted_proxies。\n"
        "         若直接暴露公网即为明文传输（协议 §1 禁止）。两种正确姿势：\n"
        "         1) 反代终结 TLS：改 host=127.0.0.1，并设置 listen.trusted_proxies=[\"127.0.0.1\"]\n"
        "         2) 网关自己终结 TLS：配置 listen.cert / listen.key",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="android-mcp-gateway")
    parser.add_argument("--config", required=True, help="配置文件路径（见 config.example.json）")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    _warn_about_tls(cfg)

    ssl_kwargs: dict[str, str] = {}
    if cfg.listen.tls_enabled:
        # 可选路径：由网关自己终结 TLS（不经反代时使用）
        ssl_kwargs = {"ssl_certfile": cfg.listen.cert, "ssl_keyfile": cfg.listen.key}

    uvicorn.run(
        create_app(cfg),
        host=cfg.listen.host,
        port=cfg.listen.port,
        **ssl_kwargs,
    )
