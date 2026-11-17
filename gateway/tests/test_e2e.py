"""端到端冒烟：假设备 WS + 真 MCP Streamable HTTP 客户端。

覆盖：配对凭证认证/限速、hello 版本与自报身份校验、在线同名唯一性（NAME_IN_USE）、
tools/list 静态目录、call 成功路由（structuredContent）、离线/未知设备语义、MCP 鉴权。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator

import httpx2
import pytest
import uvicorn
import websockets

from gateway.config import GatewayConfig, ListenConfig, McpConfig
from gateway.main import create_app

DEVICE_TOKEN = "d" * 32  # 统一配对凭证
MCP_TOKEN = "m" * 32


def make_cfg(tmp_path) -> GatewayConfig:
    return GatewayConfig(
        listen=ListenConfig(host="127.0.0.1", port=0),
        mcp=McpConfig(path="/mcp", bearer=MCP_TOKEN),
        pairing_token=DEVICE_TOKEN,
        audit_log=str(tmp_path / "audit.jsonl"),
    )


@contextlib.asynccontextmanager
async def running_gateway(tmp_path) -> AsyncIterator[int]:
    app = create_app(make_cfg(tmp_path))
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield port
    finally:
        server.should_exit = True
        await task


async def _device(port: int, token: str = DEVICE_TOKEN, device_id: str = "testdev",
                  toolset_version: int = 1):
    ws = await websockets.connect(
        f"ws://127.0.0.1:{port}/device/ws",
        additional_headers={"Authorization": f"Bearer {token}"},
    )
    hello = {
        "type": "hello",
        "id": "h-1",
        "payload": {
            "proto_version": 1,
            "device_id": device_id,
            "toolset_version": toolset_version,
            "device": {"model": "test", "android": 34, "app_version": "0.1.0", "capabilities": []},
        },
    }
    await ws.send(json.dumps(hello))
    return ws


async def _device_serving_calls(port: int, device_id: str = "testdev"):
    """假设备：hello 后把收到的帧投进队列（测试自行回 tool_result）。"""
    ws = await _device(port, device_id=device_id)
    queue: asyncio.Queue = asyncio.Queue()

    async def pump() -> None:
        try:
            async for raw in ws:
                await queue.put(json.loads(raw))
        except websockets.ConnectionClosed:
            pass

    task = asyncio.create_task(pump())
    ack = await asyncio.wait_for(queue.get(), 5)
    assert ack["type"] == "hello_ack"
    return ws, queue, task


@contextlib.asynccontextmanager
async def mcp_client(port: int):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {MCP_TOKEN}"})
    try:
        async with streamable_http_client(
            f"http://127.0.0.1:{port}/mcp", http_client=http
        ) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                yield session
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_auth_fail_and_ratelimit(tmp_path):
    async with running_gateway(tmp_path) as port:
        for _ in range(5):
            with pytest.raises(websockets.InvalidStatus):
                await _device(port, token="wrong" + "x" * 20)
        # 第 6 次：即使凭证正确也被封禁
        with pytest.raises(websockets.InvalidStatus):
            await _device(port)


@pytest.mark.asyncio
async def test_hello_toolset_mismatch(tmp_path):
    async with running_gateway(tmp_path) as port:
        ws = await _device(port, toolset_version=99)
        first = json.loads(await ws.recv())
        assert first["type"] == "error" and first["payload"]["code"] == "TOOLSET_MISMATCH"
        # close 4000
        with pytest.raises(websockets.ConnectionClosed) as exc:
            async for _ in ws:
                pass
        assert exc.value.rcvd.code == 4000


@pytest.mark.asyncio
async def test_hello_name_uniqueness(tmp_path):
    """在线同名唯一性：第二台报 NAME_IN_USE + close 4000（协议 §2/§6）。"""
    async with running_gateway(tmp_path) as port:
        first = await _device(port, device_id="mi14")
        ack = json.loads(await first.recv())
        assert ack["type"] == "hello_ack"

        second = await _device(port, device_id="mi14")
        err = json.loads(await second.recv())
        assert err["type"] == "error" and err["payload"]["code"] == "NAME_IN_USE"
        with pytest.raises(websockets.ConnectionClosed) as exc:
            async for _ in second:
                pass
        assert exc.value.rcvd.code == 4000

        # 第一台不受影响
        await first.send(json.dumps({"type": "heartbeat", "id": "hb-1", "payload": {}}))
        assert json.loads(await first.recv())["type"] == "heartbeat_ack"


@pytest.mark.asyncio
async def test_mcp_list_and_call_roundtrip(tmp_path):
    async with running_gateway(tmp_path) as port:
        device_ws, queue, pump_task = await _device_serving_calls(port, device_id="redmi")

        async with mcp_client(port) as session:
            tools = await session.list_tools()
            assert {t.name for t in tools.tools} == {
                "calendar_list_calendars",
                "calendar_list_events",
                "calendar_create_event",
                "calendar_update_event",
                "calendar_delete_event",
                "notification_read",
                "notification_dismiss",
                "notification_post",
                "device_status",
                "list_devices",           # 网关本地工具
                "generate_pairing_code",  # 网关本地工具（配对）
            }
            # MCP 层注入的 device 参数：必填自由字符串（自报登记制，无 enum）
            create = next(t for t in tools.tools if t.name == "calendar_create_event")
            device_prop = create.input_schema["properties"]["device"]
            assert device_prop["type"] == "string" and "enum" not in device_prop
            assert "device" in create.input_schema["required"]

            # 成功路径：call → 假设备回 tool_result → structuredContent 透传
            call_task = asyncio.create_task(
                session.call_tool(
                    "calendar_create_event",
                    {
                        "device": "redmi",
                        "title": "聚会",
                        "start_time": "2026-11-21T19:00:00+08:00",
                        "end_time": "2026-11-21T21:00:00+08:00",
                        "reminders": [1440, {"minutes": 30, "method": 4}],
                    },
                )
            )
            tool_call = await asyncio.wait_for(queue.get(), 10)
            assert tool_call["payload"]["tool"] == "calendar_create_event"
            # 转发前 device 参数已剥离（设备契约不含该字段）
            assert "device" not in tool_call["payload"]["args"]
            await device_ws.send(
                json.dumps(
                    {
                        "type": "tool_result",
                        "id": tool_call["id"],
                        "payload": {"ok": True, "result": {"event_id": 4821}},
                    }
                )
            )
            result = await call_task
            assert not result.is_error
            assert result.structured_content == {"event_id": 4821}

        pump_task.cancel()
        await device_ws.close()


@pytest.mark.asyncio
async def test_device_routing_and_offline(tmp_path):
    async with running_gateway(tmp_path) as port:
        # 设备从不连接：列表照常完整（静态目录）
        async with mcp_client(port) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert len(tools.tools) == 11

            # list_devices：网关本地工具，无设备时也能回答，无 device 参数
            result = await session.call_tool("list_devices", {})
            assert not result.is_error
            assert result.structured_content["online"] == []
            assert "没有设备在线" in result.structured_content["note"]

            # 未登记/离线 → DEVICE_OFFLINE 且附在线名单（供 AI 自纠）
            result = await session.call_tool("device_status", {"device": "ghost"})
            assert result.is_error
            assert "DEVICE_OFFLINE" in result.content[0].text
            assert "在线" in result.content[0].text

            # device 缺失 → ARGS_INVALID（schema 层必填）
            result = await session.call_tool("device_status", {})
            assert result.is_error
            assert "ARGS_INVALID" in result.content[0].text


@pytest.mark.asyncio
async def test_list_devices_reports_online_and_offline(tmp_path):
    """list_devices 要能区分在线设备与"连过但已离线"的设备。"""
    async with running_gateway(tmp_path) as port:
        ws, queue, pump = await _device_serving_calls(port, device_id="redmi")
        async with mcp_client(port) as session:
            await session.initialize()
            result = await session.call_tool("list_devices", {})
            body = result.structured_content
            assert body["online"] == ["redmi"]
            entry = body["devices"][0]
            assert entry["online"] is True and entry["model"] == "test"

        pump.cancel()
        await ws.close()
        await asyncio.sleep(0.4)

        async with mcp_client(port) as session:
            await session.initialize()
            body = (await session.call_tool("list_devices", {})).structured_content
            assert body["online"] == []
            entry = body["devices"][0]
            assert entry["device"] == "redmi"
            assert entry["online"] is False
            assert entry["model"] == "test"  # 历史记录保留机型
            assert entry["offline_for_s"] is not None


@pytest.mark.asyncio
async def test_mcp_requires_bearer(tmp_path):
    async with running_gateway(tmp_path) as port:
        import httpx

        async with httpx.AsyncClient() as client:
            resp = await client.post(f"http://127.0.0.1:{port}/mcp", json={})
            assert resp.status_code == 401
