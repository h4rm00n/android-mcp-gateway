"""设备侧 WS 端点 /device/ws（协议 §2 认证、§3 封装、§4-5 心跳离线、§6 身份、§8-9 重连/close code）。"""

from __future__ import annotations

import asyncio
import hmac
import json
import re
import time
import uuid

from starlette.websockets import WebSocket, WebSocketDisconnect

from .audit import AuditLog
from .catalog import PROTO_VERSION, TOOLSET_VERSION
from .config import GatewayConfig
from .errors import DeviceGone
from .netutil import client_ip
from .ratelimit import AuthRateLimiter
from .registry import DeviceRegistry, DeviceSession

HELLO_TIMEOUT_S = 10
HEARTBEAT_INTERVAL_MS = 30_000
SILENCE_MULTIPLIER = 3  # 静默 ≥ 3×interval 判离线
MAX_FRAME_BYTES = 256 * 1024

# 自报设备名：2-32 个非空白字符（协议 §6）
DEVICE_ID_RE = re.compile(r"\S{2,32}")

CLOSE_PROTOCOL_ERROR = 4000
CLOSE_REPLACED = 4001
CLOSE_SILENT = 4003


class _ProtocolViolation(Exception):
    pass


def _envelope(type_: str, id_: str, payload: dict) -> dict:
    return {"type": type_, "id": id_, "ts": int(time.time() * 1000), "payload": payload}


def _parse(text: str) -> tuple[str, str, dict]:
    if len(text.encode("utf-8")) > MAX_FRAME_BYTES:
        raise _ProtocolViolation("帧超限")
    try:
        msg = json.loads(text)
    except ValueError as e:
        raise _ProtocolViolation("非 JSON 帧") from e
    if not isinstance(msg, dict) or not isinstance(msg.get("type"), str) or not isinstance(msg.get("payload", {}), dict):
        raise _ProtocolViolation("封装不合法")
    id_ = msg.get("id")
    if id_ is not None and not isinstance(id_, str):
        raise _ProtocolViolation("id 非字符串")
    return msg["type"], id_ or "", msg.get("payload") or {}


async def _safe_close(ws: WebSocket, code: int) -> None:
    try:
        await ws.close(code=code)
    except Exception:  # 对端已断等场景
        pass


async def _send_error(ws: WebSocket, code: str, message: str, req_id: str = "") -> None:
    try:
        await ws.send_json(_envelope("error", req_id, {"code": code, "message": message}))
    except Exception:
        pass


async def _watchdog(session: DeviceSession) -> None:
    interval_s = session.heartbeat_interval_ms / 1000
    while True:
        await asyncio.sleep(interval_s)
        if time.monotonic() - session.last_seen >= interval_s * SILENCE_MULTIPLIER:
            await _safe_close(session.ws, CLOSE_SILENT)
            return


def make_device_ws_endpoint(
    cfg: GatewayConfig,
    registry: DeviceRegistry,
    limiter: AuthRateLimiter,
    audit: AuditLog,
):
    async def endpoint(websocket: WebSocket) -> None:
        ip = client_ip(websocket, cfg)

        # ---- 认证（upgrade 阶段）：统一配对凭证，只管准入不管身份 ----
        auth_header = websocket.headers.get("authorization", "")
        token_bytes = (auth_header[7:] if auth_header.startswith("Bearer ") else "").encode("utf-8")
        token_ok = hmac.compare_digest(token_bytes, cfg.pairing_token.encode("utf-8"))
        if limiter.banned(ip) or not token_ok:
            limiter.record_fail(ip)
            await audit.log("auth_fail", ip=ip)
            await _safe_close(websocket, 1008)  # 握手前拒绝，uvicorn 表现为 HTTP 403
            return
        limiter.record_success(ip)
        await audit.log("auth_ok", ip=ip)

        await websocket.accept()

        # ---- 首帧必须是 hello（协议 §4，10 秒窗口）----
        m_id = ""
        try:
            text = await asyncio.wait_for(websocket.receive_text(), HELLO_TIMEOUT_S)
            m_type, m_id, payload = _parse(text)
        except (asyncio.TimeoutError, WebSocketDisconnect, _ProtocolViolation, ValueError, KeyError):
            await _safe_close(websocket, CLOSE_PROTOCOL_ERROR)
            return

        if m_type != "hello":
            await _send_error(websocket, "PROTOCOL_ERROR", "首帧必须是 hello", m_id)
            await _safe_close(websocket, CLOSE_PROTOCOL_ERROR)
            return
        if (
            payload.get("proto_version") != PROTO_VERSION
            or payload.get("toolset_version") != TOOLSET_VERSION
        ):
            await _send_error(
                websocket,
                "TOOLSET_MISMATCH",
                f"版本不匹配：网关 proto={PROTO_VERSION} toolset={TOOLSET_VERSION}，"
                f"设备 proto={payload.get('proto_version')} toolset={payload.get('toolset_version')}",
                m_id,
            )
            await audit.log("auth_fail", ip=ip, reason="toolset_mismatch")
            await _safe_close(websocket, CLOSE_PROTOCOL_ERROR)
            return

        # ---- 自报身份（协议 §6）：合法性 + 在线唯一性 ----
        device_id = payload.get("device_id")
        if not isinstance(device_id, str) or not DEVICE_ID_RE.fullmatch(device_id):
            await _send_error(
                websocket, "PROTOCOL_ERROR", "hello.device_id 不合法（2-32 个非空白字符）", m_id
            )
            await _safe_close(websocket, CLOSE_PROTOCOL_ERROR)
            return
        if registry.get(device_id) is not None:
            # 在线唯一性：同名会话已存在 → 拒绝，设备须置 fatal 等人工改名（防互踢死循环）
            await _send_error(
                websocket, "NAME_IN_USE", f"设备名 {device_id} 已被在线会话占用，请改名", m_id
            )
            await audit.log("auth_fail", ip=ip, device_id=device_id, reason="name_in_use")
            await _safe_close(websocket, CLOSE_PROTOCOL_ERROR)
            return

        # ---- 注册会话（同名重连顶替旧会话，协议 §8）----
        device_meta = payload.get("device") if isinstance(payload.get("device"), dict) else {}
        session = DeviceSession(
            device_id=device_id,
            ws=websocket,
            session_id=uuid.uuid4().hex,
            heartbeat_interval_ms=HEARTBEAT_INTERVAL_MS,
            model=str(device_meta.get("model", "") or ""),
            android=int(device_meta.get("android", 0) or 0),
            app_version=str(device_meta.get("app_version", "") or ""),
        )
        old = registry.put(session)
        if old is not None:
            await _safe_close(old.ws, CLOSE_REPLACED)

        await websocket.send_json(
            _envelope(
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
        watchdog = asyncio.create_task(_watchdog(session))
        try:
            while True:
                text = await websocket.receive_text()
                m_type, m_id, payload = _parse(text)
                session.touch()  # 任何帧都算活性（协议 §5）

                if m_type == "heartbeat":
                    await websocket.send_json(_envelope("heartbeat_ack", m_id, {}))
                elif m_type == "tool_result":
                    fut = session.pending.pop(m_id, None)
                    if fut is not None and not fut.done():
                        fut.set_result(payload)
                    else:
                        await audit.log("late_result", device_id=session.device_id, call_id=m_id)
                elif m_type == "error":
                    await audit.log("device_error", device_id=session.device_id, call_id=m_id, detail=payload)
                else:
                    raise _ProtocolViolation(f"unexpected type {m_type}")
        except WebSocketDisconnect:
            pass
        except RuntimeError:
            pass  # 断连后 send 抛出，视同断连
        except (_ProtocolViolation, ValueError, KeyError):
            await _send_error(websocket, "PROTOCOL_ERROR", "协议违规", m_id)
            await _safe_close(websocket, CLOSE_PROTOCOL_ERROR)
        finally:
            watchdog.cancel()
            registry.pop(session.device_id, session)
            # 在途调用立即失败——离线是一等状态，不傻等超时（协议 §5）
            for fut in session.pending.values():
                if not fut.done():
                    fut.set_exception(DeviceGone())
            session.pending.clear()
            await audit.log("offline", device_id=device_id, ip=ip)

    return endpoint
