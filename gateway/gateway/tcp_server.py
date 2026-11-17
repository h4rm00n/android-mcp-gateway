"""裸 TCP 传输服务端（Noise 加密）。

与 WS 传输并存，共用 session.py 的消息循环与 registry/audit/限流。

线上首字节决定模式：
    0x01 enroll  → 配对（HMAC 挑战应答，明文帧）
    0x02 session → Noise_KK 握手（设备先发 8 字节公钥 tag 定位身份）

身份 = 静态公钥；设备名只是可变标签，改名不影响连接。
"""

from __future__ import annotations

import asyncio

from .audit import AuditLog
from .channel import NoiseChannel
from .keystore import KeyStore, device_tag  # noqa: F401 - device_tag 供测试与外部使用
from .noise_wire import (
    HANDSHAKE_TIMEOUT_S,
    MODE_ENROLL,
    MODE_SESSION,
    REJECT_DEVICE_REVOKED,
    TAG_LEN,
    WireError,
    enroll,
    server_handshake,
    write_reject_frame,
)
from .pairing import PairingManager
from .ratelimit import AuthRateLimiter
from .registry import DeviceRegistry
from .session import run_device_session


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

            # 按公钥标签定位——改名后 tag 不变，因此仍能连上
            matched = store.find_by_tag(tag)
            if matched is None:
                if store.is_revoked(tag):
                    # 曾经配对过、已被吊销：回一个明确原因，设备据此停机等人工重新配对。
                    # 否则设备只会看到连接被关，当成网络抖动无限重试。
                    #
                    # 关键：这里**不**调用 limiter.record_fail()。理由有三：
                    #  ① 这不是"猜密码失败"——tag 已唯一确定是哪台设备，没有暴力破解空间，
                    #     限速对它没有防护价值；
                    #  ② 记失败会让 NAT 后面的其它正常设备跟着被封 10 分钟，等于拿整条出口
                    #     网段当惩罚对象（一台旧 APK 重试 5 次就够触发）；
                    #  ③ 封禁后连拒绝帧都发不出去（banned 分支直接断开），恰好把我们要
                    #     传达的那条信息弄丢了——设备只能干等到解封。
                    await audit.log("auth_fail", ip=ip, reason="device_revoked")
                    await write_reject_frame(
                        writer,
                        REJECT_DEVICE_REVOKED,
                        "该设备已被网关吊销，需要在网关上重新生成配对码再配对",
                    )
                    raise WireError("设备已被吊销")
                # 从没配对过的 tag：静默断开，不向未认证对端确认任何信息。
                # 这条路径才需要限速——tag 空间可枚举，是真正的探测面。
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
                allow_rename=True,  # 名字只是标签，设备可自报新名（在线唯一仍校验）
                keystore=store,
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
