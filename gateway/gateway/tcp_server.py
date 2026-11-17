"""裸 TCP 传输服务端（Noise 加密）。

与 WS 传输并存，共用 session.py 的消息循环与 registry/audit/限流。

线上首字节决定模式：
    0x01 enroll  → 配对（HMAC 挑战应答，明文帧）
    0x02 session → Noise_KK 握手（设备先发 8 字节 tag 定位公钥）
"""

from __future__ import annotations

import asyncio
import hashlib

from .audit import AuditLog
from .channel import NoiseChannel
from .keystore import KeyStore
from .noise_wire import (
    HANDSHAKE_TIMEOUT_S,
    MODE_ENROLL,
    MODE_SESSION,
    TAG_LEN,
    WireError,
    server_handshake,
    enroll,
    read_message,
)
from .pairing import PairingManager
from .ratelimit import AuthRateLimiter
from .registry import DeviceRegistry
from .session import run_device_session


def device_tag(name: str) -> bytes:
    """设备名 → 8 字节定位标签（哈希前缀，不泄露明文设备名）。"""
    return hashlib.sha256(name.encode("utf-8")).digest()[:TAG_LEN]


async def _handle_conn(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    store: KeyStore,
    pairing: PairingManager,
    registry: DeviceRegistry,
    limiter: AuthRateLimiter,
    audit: AuditLog,
) -> None:
    peer = writer.get_extra_info("peername")
    ip = peer[0] if peer else "?"
    try:
        mode_bytes = await asyncio.wait_for(reader.readexactly(1), HANDSHAKE_TIMEOUT_S)
    except (asyncio.TimeoutError, asyncio.IncompleteReadError):
        writer.close()
        return

    mode = mode_bytes[0]
    try:
        if mode == MODE_ENROLL:
            if limiter.banned(ip):
                raise WireError("该 IP 暂被封禁")
            try:
                name, _pub = await asyncio.wait_for(
                    enroll(reader, writer, store, pairing, audit, ip), 60
                )
            except asyncio.TimeoutError as e:
                raise WireError("配对超时") from e
            await audit.log("enroll_ok", ip=ip, device_id=name)
            # 配对完成后**不**在同一连接上开会话：强制设备用新密钥重连
            # （避免复用可能的旁路状态，也让设备确认落盘成功）
            return

        if mode == MODE_SESSION:
            if limiter.banned(ip):
                raise WireError("该 IP 暂被封禁")
            try:
                tag = await asyncio.wait_for(reader.readexactly(TAG_LEN), HANDSHAKE_TIMEOUT_S)
            except asyncio.TimeoutError as e:
                raise WireError("读取设备标签超时") from e

            matched = next(
                (d for d in store.devices() if device_tag(d.name) == tag),
                None,
            )
            if matched is None:
                limiter.record_fail(ip)
                await audit.log("auth_fail", ip=ip, reason="unknown_device")
                raise WireError("未配对的设备")

            try:
                conn = await asyncio.wait_for(
                    server_handshake(reader, writer, store.gateway_private, matched.public_key),
                    HANDSHAKE_TIMEOUT_S,
                )
            except asyncio.TimeoutError as e:
                limiter.record_fail(ip)
                await audit.log("auth_fail", ip=ip, device_id=matched.name, reason="handshake_timeout")
                raise WireError("Noise 握手超时") from e

            limiter.record_success(ip)
            await audit.log("auth_ok", ip=ip, device_id=matched.name, transport="tcp+noise")
            await run_device_session(
                NoiseChannel(conn, reader, writer),
                registry,
                audit,
                ip,
                expected_device_id=matched.name,
            )
            return

        await audit.log("auth_fail", ip=ip, reason=f"unknown_mode_{mode}")
        raise WireError("未知传输模式")
    except WireError:
        pass
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass


async def start_tcp_server(
    host: str,
    port: int,
    store: KeyStore,
    pairing: PairingManager,
    registry: DeviceRegistry,
    limiter: AuthRateLimiter,
    audit: AuditLog,
) -> asyncio.AbstractServer:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _handle_conn(reader, writer, store, pairing, registry, limiter, audit)

    return await asyncio.start_server(handler, host, port)
