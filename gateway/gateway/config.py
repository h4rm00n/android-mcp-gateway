"""配置加载。格式见 docs/protocol.md §10 与 config.example.json。"""

from __future__ import annotations

import ipaddress
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ListenConfig:
    host: str = "0.0.0.0"
    port: int = 443
    cert: str | None = None
    key: str | None = None
    # 反代 IP/CIDR：来自这些地址的连接才信任 X-Forwarded-For（nginx 终结 TLS 时必需，
    # 否则所有设备在限速器/审计里都显示成 127.0.0.1，一台被打就全员被封）
    trusted_proxies: tuple[str, ...] = field(default_factory=tuple)

    @property
    def tls_enabled(self) -> bool:
        """网关自己是否终结 TLS（false 表示明文，预期由反代终结）。"""
        return bool(self.cert and self.key)

    @property
    def behind_proxy(self) -> bool:
        return bool(self.trusted_proxies)

    def is_trusted_proxy(self, peer: str) -> bool:
        try:
            addr = ipaddress.ip_address(peer)
        except ValueError:
            return False
        for entry in self.trusted_proxies:
            try:
                if addr in ipaddress.ip_network(entry, strict=False):
                    return True
            except ValueError:
                continue
        return False


@dataclass(frozen=True)
class McpConfig:
    path: str = "/mcp"
    bearer: str = ""
    # Host 头白名单（DNS rebinding 防护）。反代场景填对外域名/IP；留空则关闭该校验。
    allowed_hosts: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class TcpConfig:
    """裸 TCP + Noise 传输（实验分支）。与 WS 传输可同时开启。"""

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 18438
    keystore: str = "./gateway.keys"  # 网关静态密钥 + 已配对设备公钥（0600）
    pairing_ttl_s: int = 300


@dataclass(frozen=True)
class GatewayConfig:
    listen: ListenConfig
    mcp: McpConfig
    pairing_token: str  # 设备侧统一配对凭证（不承载身份，身份=自报 device_id）
    audit_log: str = "./audit.jsonl"
    audit_max_bytes: int = 10 * 1024 * 1024
    audit_backups: int = 5
    tcp: TcpConfig = field(default_factory=TcpConfig)


def _as_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(v) for v in value)  # type: ignore[union-attr]


def load_config(path: str | Path) -> GatewayConfig:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))

    listen_raw = dict(raw.get("listen", {}))
    listen_raw["trusted_proxies"] = _as_tuple(listen_raw.get("trusted_proxies"))
    listen = ListenConfig(**listen_raw)

    mcp_raw = dict(raw.get("mcp", {}))
    mcp_raw["allowed_hosts"] = _as_tuple(mcp_raw.get("allowed_hosts"))
    mcp = McpConfig(**mcp_raw)

    pairing_token = raw.get("pairing_token", "")

    if not mcp.bearer:
        sys.exit("config error: mcp.bearer 不能为空")
    if not pairing_token:
        sys.exit("config error: pairing_token 不能为空")

    for label, tok in (("mcp.bearer", mcp.bearer), ("pairing_token", pairing_token)):
        if len(tok) < 16:
            print(
                f"warning: {label} 短于 16 字符，建议 openssl rand -hex 32"
                "（失败限速是弱 token 的主防线）",
                file=sys.stderr,
            )

    tcp_raw = raw.get("tcp", {}) or {}
    tcp = TcpConfig(
        enabled=bool(tcp_raw.get("enabled", False)),
        host=str(tcp_raw.get("host", "127.0.0.1")),
        port=int(tcp_raw.get("port", 18438)),
        keystore=str(tcp_raw.get("keystore", "./gateway.keys")),
        pairing_ttl_s=int(tcp_raw.get("pairing_ttl_s", 300)),
    )

    return GatewayConfig(
        listen=listen,
        mcp=mcp,
        pairing_token=pairing_token,
        audit_log=raw.get("audit_log", "./audit.jsonl"),
        audit_max_bytes=int(raw.get("audit_max_bytes", 10 * 1024 * 1024)),
        audit_backups=int(raw.get("audit_backups", 5)),
        tcp=tcp,
    )
