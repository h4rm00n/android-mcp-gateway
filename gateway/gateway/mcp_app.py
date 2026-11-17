"""MCP 服务端：静态目录 tools/list + tools/call 路由到设备 WS 会话（协议 §7）。

工具分两类：
- **设备工具**：目录在 catalog.py，调用转发到设备 WS 会话。入参强制携带 `device`，
  网关按它路由并在转发前剥离（设备契约/TOOLSET_VERSION 不含该字段）。
- **网关本地工具**（`list_devices`）：在网关内直接回答，不经过设备，仅用于让 AI
  发现"有哪些设备、谁在线"，避免让 AI 猜设备名。

mcp SDK 2.x：低层 Server 用构造器注册 handler（无装饰器）；类型字段为
snake_case，线上序列化由 SDK 按 alias 输出 camelCase。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from copy import deepcopy

from mcp.server.lowlevel import Server
from mcp.server.context import ServerRequestContext
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    TextContent,
    Tool,
)

from .audit import AuditLog
from .catalog import GATEWAY_LOCAL_TOOLS, TOOLS, timeout_for
from .pairing import PairingManager, format_code
from .config import GatewayConfig
from .errors import DeviceCallTimeout, DeviceGone
from .registry import DeviceRegistry, DeviceSession

MAX_CALL_BYTES = 256 * 1024
MAX_TIMEOUT_MS = 120_000
LOCAL_TOOL_NAMES = frozenset(t["name"] for t in GATEWAY_LOCAL_TOOLS)


def _err_result(code: str, message: str) -> CallToolResult:
    body = {"error": {"code": code, "message": message}}
    return CallToolResult(
        is_error=True,
        content=[TextContent(type="text", text=json.dumps(body, ensure_ascii=False))],
    )


def _ok_result(result: dict) -> CallToolResult:
    text = json.dumps(result, ensure_ascii=False)
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=result,
    )


async def _call_device(
    session: DeviceSession,
    device_id: str,
    audit: AuditLog,
    tool: str,
    args: dict,
    timeout_ms: int,
) -> dict:
    call_id = "c-" + uuid.uuid4().hex[:12]
    frame = json.dumps(
        {
            "type": "tool_call",
            "id": call_id,
            "ts": int(time.time() * 1000),
            "payload": {"tool": tool, "args": args, "timeout_ms": timeout_ms},
        },
        ensure_ascii=False,
    )
    if len(frame.encode("utf-8")) > MAX_CALL_BYTES:
        raise ValueError("调用参数超过 256KB 帧上限")

    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    session.pending[call_id] = fut
    try:
        await session.ws.send_text(frame)
    except Exception:
        session.pending.pop(call_id, None)
        raise DeviceGone()  # 发送失败≈会话已死

    try:
        return await asyncio.wait_for(fut, timeout=timeout_ms / 1000)
    except asyncio.TimeoutError:
        session.pending.pop(call_id, None)
        await audit.log("timeout", device_id=device_id, call_id=call_id, tool=tool)
        raise DeviceCallTimeout(call_id, tool) from None


def build_mcp_server(
    cfg: GatewayConfig,
    registry: DeviceRegistry,
    audit: AuditLog,
    pairing: PairingManager | None = None,
) -> Server:
    def _with_device_param(schema: dict) -> dict:
        """给工具 schema 注入必填 device 参数（仅 MCP 层，设备端 schema 不含它）。

        自报登记制下设备名单是动态的，故不带 enum（协议 §6）；AI 从上下文获知
        名字，或从 DEVICE_OFFLINE 错误附带的在线名单自纠。
        """
        s = deepcopy(schema)
        s["properties"]["device"] = {
            "type": "string",
            "minLength": 2,
            "maxLength": 32,
            "description": "目标设备名（必填，登记时的名字，区分大小写）。离线或不存在会报 DEVICE_OFFLINE 并附当前在线名单",
        }
        s["required"] = list(s.get("required", [])) + ["device"]
        return s

    async def on_list_tools(
        ctx: ServerRequestContext, params: object
    ) -> ListToolsResult:
        # 静态目录，与设备在线状态解耦（AGENTS.md「工具目录静态化」）
        tools = [
            Tool(
                name=t["name"],
                description=t["description"],
                input_schema=_with_device_param(t["inputSchema"]),
            )
            for t in TOOLS
        ]
        # 网关本地工具：不带 device 参数（它本身就是用来发现设备的）
        tools += [
            Tool(name=t["name"], description=t["description"], input_schema=t["inputSchema"])
            for t in GATEWAY_LOCAL_TOOLS
        ]
        return ListToolsResult(tools=tools)

    async def on_call_tool(
        ctx: ServerRequestContext, params: CallToolRequestParams
    ) -> CallToolResult:
        tool = params.name

        # ---- 网关本地工具：不经过设备 ----
        if tool == "generate_pairing_code":
            if pairing is None:
                return _err_result(
                    "TOOL_NOT_FOUND",
                    "配对码功能未启用（网关未开启 tcp.enabled）",
                )
            args_raw = dict(params.arguments or {})
            label = str(args_raw.get("device_name", "") or "").strip()
            code = pairing.issue(label)
            await audit.log("pairing_code_issued", device_id=label or None)
            return _ok_result(
                {
                    "code": code,
                    "display": format_code(code),
                    "device_name": label or None,
                    "expires_in_s": cfg.tcp.pairing_ttl_s,
                    "pending_codes": pairing.pending_count(),
                    "usage": (
                        "把 display 里的码填到手机 App 的「配对码」输入框，点配对。"
                        "码 5 分钟内有效、只能用一次。"
                    ),
                }
            )

        if tool == "list_devices":
            devices = registry.snapshot()
            online = [d["device"] for d in devices if d["online"]]
            await audit.log("local_call", tool=tool, online=online)
            return _ok_result(
                {
                    "devices": devices,
                    "online": online,
                    "note": (
                        "调用其它工具时把 device 设为 devices[].device 中的值"
                        if online
                        else "当前没有设备在线；设备需在 App 中启动服务"
                    ),
                }
            )
        if tool in LOCAL_TOOL_NAMES:
            return _err_result("TOOL_NOT_FOUND", f"本地工具 {tool} 未实现")

        timeout_ms = min(timeout_for(tool), MAX_TIMEOUT_MS)

        # device 路由参数（强制）：从转发参数中剥离
        args = dict(params.arguments or {})
        device = args.pop("device", None)
        if not isinstance(device, str) or not device.strip():
            return _err_result("ARGS_INVALID", "device 必填：目标设备名（登记时的名字）")

        await audit.log("call", device_id=device, tool=tool)
        session = registry.get(device)
        if session is None:
            online = ", ".join(registry.online_names()) or "无"
            return _err_result(
                "DEVICE_OFFLINE", f"设备 {device} 离线或不存在；当前在线: {online}"
            )

        try:
            payload = await _call_device(session, device, audit, tool, args, timeout_ms)
        except DeviceGone as e:
            return _err_result(e.code, e.message)
        except DeviceCallTimeout:
            return _err_result("TIMEOUT", f"设备执行超时（>{timeout_ms}ms）")

        if payload.get("ok"):
            result = payload.get("result") or {}
            await audit.log("result", device_id=device, tool=tool, ok=True)
            return _ok_result(result)
        error = payload.get("error") or {}
        await audit.log("result", device_id=device, tool=tool, ok=False, code=error.get("code"))
        return _err_result(str(error.get("code", "DEVICE_INTERNAL")), str(error.get("message", "设备执行失败")))

    return Server(
        "android-mcp-gateway",
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )
