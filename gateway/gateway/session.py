"""设备会话逻辑：传输无关的消息循环（hello / 心跳 / tool_call 分发 / 离线语义）。

WS 与裸 TCP(Noise) 两种传输都调用 `run_device_session()`，区别只在传入的 Channel。
协议语义见 docs/protocol.md §4-§8。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid

from .audit import AuditLog
from .catalog import PROTO_VERSION, TOOLSET_VERSION
from .channel import Channel, Closed
from .errors import DeviceGone
from .registry import DeviceRegistry, DeviceSession

HELLO_TIMEOUT_S = 10
HEARTBEAT_INTERVAL_MS = 30_000
SILENCE_MULTIPLIER = 3
MAX_FRAME_BYTES = 256 * 1024
DEVICE_ID_RE = re.compile(r"\S{2,32}")

CLOSE_PROTOCOL_ERROR = 4000
CLOSE_REPLACED = 4001
CLOSE_SILENT = 4003


class ProtocolViolation(Exception):
    pass


def envelope(type_: str, id_: str, payload: dict) -> dict:
    return {"type": type_, "id": id_, "ts": int(time.time() * 1000), "payload": payload}


def parse_frame(text: str) -> tuple[str, str, dict]:
    if len(text.encode("utf-8")) > MAX_FRAME_BYTES:
        raise ProtocolViolation("帧超限")
    try:
        msg = json.loads(text)
    except ValueError as e:
        raise ProtocolViolation("非 JSON 帧") from e
    if (
        not isinstance(msg, dict)
        or not isinstance(msg.get("type"), str)
        or not isinstance(msg.get("payload", {}), dict)
    ):
        raise ProtocolViolation("封装不合法")
    id_ = msg.get("id")
    if id_ is not None and not isinstance(id_, str):
        raise ProtocolViolation("id 非字符串")
    return msg["type"], id_ or "", msg.get("payload") or {}


async def send_error(channel: Channel, code: str, message: str, req_id: str = "") -> None:
    try:
        await channel.send_json(envelope("error", req_id, {"code": code, "message": message}))
    except Closed:
        pass


async def _watchdog(session: DeviceSession, channel: Channel) -> None:
    interval_s = session.heartbeat_interval_ms / 1000
    while True:
        await asyncio.sleep(interval_s)
        if time.monotonic() - session.last_seen >= interval_s * SILENCE_MULTIPLIER:
            await channel.close(CLOSE_SILENT)
            return


async def run_device_session(
    channel: Channel,
    registry: DeviceRegistry,
    audit: AuditLog,
    ip: str,
    *,
    expected_device_id: str | None = None,
    allow_rename: bool = False,
    keystore: object | None = None,
) -> None:
    """跑完整会话：hello 校验 → 注册 → 消息循环 → 清理。

    - `expected_device_id`：传输层已确认的身份（Noise 通过公钥配对确认）。
    - `allow_rename`：允许设备自报与配对标签不同的名字（名字只是标签，身份是公钥）。
      改名会更新 keystore 里的标签；撞到别人的名字则拒绝。
    - `keystore`：allow_rename 为真时必需（用于持久化改名）。
    """
    m_id = ""
    try:
        # ---- 首帧必须是 hello（协议 §4，10 秒窗口）----
        try:
            text = await asyncio.wait_for(channel.receive_text(), HELLO_TIMEOUT_S)
            m_type, m_id, payload = parse_frame(text)
        except (asyncio.TimeoutError, Closed, ProtocolViolation, ValueError, KeyError):
            await channel.close(CLOSE_PROTOCOL_ERROR)
            return

        if m_type != "hello":
            await send_error(channel, "PROTOCOL_ERROR", "首帧必须是 hello", m_id)
            await channel.close(CLOSE_PROTOCOL_ERROR)
            return

        if (
            payload.get("proto_version") != PROTO_VERSION
            or payload.get("toolset_version") != TOOLSET_VERSION
        ):
            await send_error(
                channel,
                "TOOLSET_MISMATCH",
                f"版本不匹配：网关 proto={PROTO_VERSION} toolset={TOOLSET_VERSION}，"
                f"设备 proto={payload.get('proto_version')} toolset={payload.get('toolset_version')}",
                m_id,
            )
            await audit.log("auth_fail", ip=ip, reason="toolset_mismatch")
            await channel.close(CLOSE_PROTOCOL_ERROR)
            return

        # ---- 身份：传输层已确认则用之，否则取自报值（协议 §6）----
        declared = payload.get("device_id")
        if expected_device_id is not None:
            if declared is not None and not isinstance(declared, str):
                await send_error(channel, "PROTOCOL_ERROR", "hello.device_id 类型非法", m_id)
                await channel.close(CLOSE_PROTOCOL_ERROR)
                return
            if declared is not None and declared != expected_device_id:
                if not allow_rename:
                    await send_error(
                        channel,
                        "PROTOCOL_ERROR",
                        f"hello.device_id 与配对身份不符（已配对为 {expected_device_id}）",
                        m_id,
                    )
                    await channel.close(CLOSE_PROTOCOL_ERROR)
                    return
                # 允许改名：校验格式与占用，然后更新 keystore 标签
                if not DEVICE_ID_RE.fullmatch(declared):
                    await send_error(
                        channel,
                        "PROTOCOL_ERROR",
                        "hello.device_id 不合法（2-32 个非空白字符）",
                        m_id,
                    )
                    await channel.close(CLOSE_PROTOCOL_ERROR)
                    return
                if registry.get(declared) is not None:
                    await send_error(
                        channel, "NAME_IN_USE", f"设备名 {declared} 已被在线会话占用", m_id
                    )
                    await audit.log("auth_fail", ip=ip, device_id=declared, reason="name_in_use")
                    await channel.close(CLOSE_PROTOCOL_ERROR)
                    return
                if keystore is not None:
                    try:
                        keystore.rename(expected_device_id, declared)  # type: ignore[attr-defined]
                    except (ValueError, KeyError) as e:
                        await send_error(channel, "NAME_IN_USE", str(e), m_id)
                        await audit.log(
                            "auth_fail", ip=ip, device_id=declared, reason="rename_rejected"
                        )
                        await channel.close(CLOSE_PROTOCOL_ERROR)
                        return
                await audit.log(
                    "device_renamed", ip=ip, old=expected_device_id, new=declared
                )
                device_id = declared
            else:
                device_id = expected_device_id
        else:
            if not isinstance(declared, str) or not DEVICE_ID_RE.fullmatch(declared):
                await send_error(
                    channel, "PROTOCOL_ERROR", "hello.device_id 不合法（2-32 个非空白字符）", m_id
                )
                await channel.close(CLOSE_PROTOCOL_ERROR)
                return
            device_id = declared

        if registry.get(device_id) is not None:
            await send_error(
                channel, "NAME_IN_USE", f"设备名 {device_id} 已被在线会话占用，请改名", m_id
            )
            await audit.log("auth_fail", ip=ip, device_id=device_id, reason="name_in_use")
            await channel.close(CLOSE_PROTOCOL_ERROR)
            return

        # ---- 注册会话 ----
        device_meta = payload.get("device") if isinstance(payload.get("device"), dict) else {}
        session = DeviceSession(
            device_id=device_id,
            ws=channel,  # 字段名沿用 ws，实际是 Channel
            session_id=uuid.uuid4().hex,
            heartbeat_interval_ms=HEARTBEAT_INTERVAL_MS,
            model=str(device_meta.get("model", "") or ""),
            android=int(device_meta.get("android", 0) or 0),
            app_version=str(device_meta.get("app_version", "") or ""),
        )
        old = registry.put(session)
        if old is not None:
            await old.ws.close(CLOSE_REPLACED)

        await channel.send_json(
            envelope(
                "hello_ack",
                m_id,
                {
                    "session_id": session.session_id,
                    "heartbeat_interval_ms": HEARTBEAT_INTERVAL_MS,
                    "server_time": int(time.time() * 1000),
                },
            )
        )
        await audit.log(
            "online", device_id=device_id, ip=ip, model=session.model, android=session.android
        )

        # ---- 消息循环 ----
        watchdog = asyncio.create_task(_watchdog(session, channel))
        try:
            while True:
                text = await channel.receive_text()
                m_type, m_id, payload = parse_frame(text)
                session.touch()  # 任何帧都算活性（协议 §5）

                if m_type == "heartbeat":
                    await channel.send_json(envelope("heartbeat_ack", m_id, {}))
                elif m_type == "tool_result":
                    fut = session.pending.pop(m_id, None)
                    if fut is not None and not fut.done():
                        fut.set_result(payload)
                    else:
                        await audit.log("late_result", device_id=session.device_id, call_id=m_id)
                elif m_type == "error":
                    await audit.log(
                        "device_error", device_id=session.device_id, call_id=m_id, detail=payload
                    )
                else:
                    raise ProtocolViolation(f"unexpected type {m_type}")
        except Closed:
            pass
        except (ProtocolViolation, ValueError, KeyError):
            await send_error(channel, "PROTOCOL_ERROR", "协议违规", m_id)
            await channel.close(CLOSE_PROTOCOL_ERROR)
        finally:
            watchdog.cancel()
            registry.pop(session.device_id, session)
            for fut in session.pending.values():
                if not fut.done():
                    fut.set_exception(DeviceGone())
            session.pending.clear()
            await audit.log("offline", device_id=device_id, ip=ip)
    except Closed:
        pass
