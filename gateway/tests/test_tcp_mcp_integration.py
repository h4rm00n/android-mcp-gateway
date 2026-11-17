"""完整链路集成测试：MCP 生成配对码 → 设备 TCP 配对 → Noise 会话 → MCP 工具调用路由到该设备。"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from collections.abc import AsyncIterator

import httpx2
import pytest
import uvicorn
from noise.connection import Keypair, NoiseConnection

from gateway.config import (
    GatewayConfig,
    ListenConfig,
    McpConfig,
    TcpConfig,
)
from gateway.keystore import gen_keypair
from gateway.main import create_app
from gateway.noise_wire import (
    MODE_ENROLL,
    MODE_SESSION,
    PATTERN,
    read_message,
    read_plain_frame,
    write_message,
    write_plain_frame,
)
from gateway.pairing import compute_proof, derive_key, pairing_salt
from gateway.keystore import device_tag

MCP_TOKEN = "m" * 32
DEVICE_NAME = "tcpdev"


def make_cfg(tmp_path) -> GatewayConfig:
    return GatewayConfig(
        listen=ListenConfig(host="127.0.0.1", port=0),
        mcp=McpConfig(path="/mcp", bearer=MCP_TOKEN),
        pairing_token="unused-in-tcp-mode",
        audit_log=str(tmp_path / "audit.jsonl"),
        tcp=TcpConfig(
            enabled=True,
            host="127.0.0.1",
            port=0,  # 由 uvicorn 分配；TCP 端口单独探测
            keystore=str(tmp_path / "gw.keys"),
        ),
    )


@contextlib.asynccontextmanager
async def running_gateway(tmp_path) -> AsyncIterator[tuple[int, int]]:
    """返回 (http_port, tcp_port)。"""
    cfg = make_cfg(tmp_path)
    # TCP 端口：先占用一个空闲端口再交给配置
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    tcp_port = s.getsockname()[1]
    s.close()

    cfg = GatewayConfig(
        listen=cfg.listen,
        mcp=cfg.mcp,
        pairing_token=cfg.pairing_token,
        audit_log=cfg.audit_log,
        tcp=TcpConfig(
            enabled=True,
            host="127.0.0.1",
            port=tcp_port,
            keystore=cfg.tcp.keystore,
        ),
    )
    app = create_app(cfg)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    http_port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield http_port, tcp_port
    finally:
        server.should_exit = True
        await task


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


async def pair_device(tcp_port: int, code: str, name: str = DEVICE_NAME):
    """走完配对流程，返回 (priv, pub)。"""
    reader, writer = await asyncio.open_connection("127.0.0.1", tcp_port)
    writer.write(bytes([MODE_ENROLL]))
    dev_priv, dev_pub = gen_keypair()
    await write_plain_frame(
        writer, json.dumps({"name": name, "dev_pub": base64.b64encode(dev_pub).decode()}).encode()
    )
    resp = json.loads((await read_plain_frame(reader)).decode())
    nonce = base64.b64decode(resp["nonce"])
    gw_pub = base64.b64decode(resp["gw_pub"])

    key = derive_key(code, pairing_salt(name, gw_pub))
    proof = compute_proof(key, "dev", nonce, name, dev_pub, gw_pub)
    await write_plain_frame(writer, base64.b64encode(proof))
    final = json.loads((await read_plain_frame(reader)).decode())
    assert final["ok"] is True, final
    # 反向证明可验 → 未被中间人替换
    assert base64.b64decode(final["proof"]) == compute_proof(key, "gw", nonce, name, dev_pub, gw_pub)

    writer.close()
    await writer.wait_closed()
    return dev_priv, dev_pub


async def open_session(tcp_port: int, priv: bytes, gw_pub: bytes, dev_pub: bytes, name: str = DEVICE_NAME):
    reader, writer = await asyncio.open_connection("127.0.0.1", tcp_port)
    writer.write(bytes([MODE_SESSION]) + device_tag(dev_pub))
    await writer.drain()

    conn = NoiseConnection.from_name(PATTERN)
    conn.set_as_initiator()
    conn.set_keypair_from_private_bytes(Keypair.STATIC, priv)
    conn.set_keypair_from_public_bytes(Keypair.REMOTE_STATIC, gw_pub)
    conn.start_handshake()
    await write_plain_frame(writer, conn.write_message())
    conn.read_message(await read_plain_frame(reader))
    assert conn.handshake_finished
    return conn, reader, writer


async def send(conn, writer, obj: dict) -> None:
    await write_message(writer, conn, json.dumps(obj).encode())


async def recv(conn, reader) -> dict:
    return json.loads((await read_message(reader, conn)).decode())


@pytest.mark.asyncio
async def test_full_flow_mcp_pairing_then_tool_call(tmp_path):
    async with running_gateway(tmp_path) as (http_port, tcp_port):
        async with mcp_client(http_port) as session:
            # 1) AI 调工具生成配对码
            gen = await session.call_tool("generate_pairing_code", {"device_name": DEVICE_NAME})
            assert not gen.is_error
            code = gen.structured_content["code"]
            assert len(code) == 8
            print(f"  配对码: {gen.structured_content['display']}")

            # 2) 设备完成配对
            dev_priv, dev_pub = await pair_device(tcp_port, code)
            print("  ✓ 配对成功")

            # 3) 配对后 device 出现在 list_devices（离线）
            devices = (await session.call_tool("list_devices", {})).structured_content
            print(f"  list_devices: {devices['online']}")

            # 4) 建立 Noise 会话 → hello
            gw_pub = b""  # 从配对响应拿不到，重新读 keystore 不方便；直接由派生盐反推不现实
            # 这里用协议：配对已完成，网关公钥可从 server 侧 keystore 读取
            from gateway.keystore import KeyStore

            ks = KeyStore(tmp_path / "gw.keys")
            gw_pub = ks.gateway_public

            conn, reader, writer = await open_session(tcp_port, dev_priv, gw_pub, dev_pub)
            await send(
                conn, writer,
                {
                    "type": "hello",
                    "id": "h-1",
                    "payload": {
                        "proto_version": 1,
                        "device_id": DEVICE_NAME,
                        "toolset_version": 1,
                        "device": {"model": "tcp-test", "android": 33, "app_version": "0.1.0"},
                    },
                },
            )
            ack = await recv(conn, reader)
            assert ack["type"] == "hello_ack"
            print("  ✓ Noise 会话建立，hello_ack 收到")

            # 5) MCP 调用工具 → 应通过 TCP 通道到达设备
            call_task = asyncio.create_task(
                session.call_tool("device_status", {"device": DEVICE_NAME})
            )
            tool_call = await asyncio.wait_for(recv(conn, reader), 10)
            assert tool_call["type"] == "tool_call"
            assert tool_call["payload"]["tool"] == "device_status"
            print(f"  ✓ 收到 tool_call: {tool_call['payload']['tool']}")

            # 6) 设备回结果 → MCP 拿到
            await send(
                conn, writer,
                {
                    "type": "tool_result",
                    "id": tool_call["id"],
                    "payload": {"ok": True, "result": {"model": "tcp-test", "battery_level": 88}},
                },
            )
            result = await call_task
            assert not result.is_error
            assert result.structured_content["battery_level"] == 88
            print(f"  ✓ MCP 收到结果: {result.structured_content}")

            writer.close()


@pytest.mark.asyncio
async def test_pairing_code_rejected_after_use(tmp_path):
    async with running_gateway(tmp_path) as (http_port, tcp_port):
        async with mcp_client(http_port) as session:
            gen = await session.call_tool("generate_pairing_code", {"device_name": "dev1"})
            code = gen.structured_content["code"]

            await pair_device(tcp_port, code, name="dev1")

            # 同码再次配对必须失败
            with pytest.raises((AssertionError, asyncio.IncompleteReadError, ConnectionError)):
                await pair_device(tcp_port, code, name="dev2")


@pytest.mark.asyncio
async def test_unpaired_cannot_connect(tmp_path):
    async with running_gateway(tmp_path) as (_http_port, tcp_port):
        _priv, unpaired_pub = gen_keypair()
        reader, writer = await asyncio.open_connection("127.0.0.1", tcp_port)
        writer.write(bytes([MODE_SESSION]) + device_tag(unpaired_pub))
        await writer.drain()
        data = await asyncio.wait_for(reader.read(1), 5)
        assert data == b"", "未配对设备应被直接断开"
        writer.close()
