"""配对记录维护的端到端验证：改名、撞名、吊销、陈旧条目清理。

覆盖用户关心的三个问题：
1. 配对记录如何保持（身份=公钥，名字=标签）
2. 设备改名后还能不能连（能，tag 由公钥派生）
3. 列表与吊销（list_paired_devices / unpair_device）
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import socket
from collections.abc import AsyncIterator

import httpx2
import pytest
import uvicorn
from noise.connection import Keypair, NoiseConnection

from gateway.config import GatewayConfig, ListenConfig, McpConfig, TcpConfig
from gateway.keystore import KeyStore, device_tag, gen_keypair
from gateway.main import create_app
from gateway.noise_wire import (
    FLAG_REJECT,
    MODE_ENROLL,
    MODE_SESSION,
    PATTERN,
    REJECT_DEVICE_REVOKED,
    read_message,
    read_plain_frame,
    read_plain_frame_ex,
    write_message,
    write_plain_frame,
)
from gateway.pairing import compute_proof, derive_key, pairing_salt

MCP_TOKEN = "m" * 32


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@contextlib.asynccontextmanager
async def running_gateway(tmp_path) -> AsyncIterator[tuple[int, int]]:
    tcp_port = _free_port()
    cfg = GatewayConfig(
        listen=ListenConfig(host="127.0.0.1", port=0),
        mcp=McpConfig(path="/mcp", bearer=MCP_TOKEN),
        pairing_token="unused",
        audit_log=str(tmp_path / "audit.jsonl"),
        tcp=TcpConfig(
            enabled=True, host="127.0.0.1", port=tcp_port,
            keystore=str(tmp_path / "gw.keys"),
        ),
    )
    app = create_app(cfg)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error"))
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
        async with streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=http) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                yield session
    finally:
        await http.aclose()


async def pair(tcp_port: int, code: str, name: str) -> tuple[bytes, bytes, bytes]:
    """配对，返回 (dev_priv, dev_pub, gw_pub)。"""
    reader, writer = await asyncio.open_connection("127.0.0.1", tcp_port)
    writer.write(bytes([MODE_ENROLL]))
    dev_priv, dev_pub = gen_keypair()
    await write_plain_frame(
        writer, json.dumps({"name": name, "dev_pub": base64.b64encode(dev_pub).decode()}).encode()
    )
    ch = json.loads((await read_plain_frame(reader)).decode())
    nonce, gw_pub = base64.b64decode(ch["nonce"]), base64.b64decode(ch["gw_pub"])
    key = derive_key(code, pairing_salt(name, gw_pub))
    await write_plain_frame(writer, base64.b64encode(compute_proof(key, "dev", nonce, name, dev_pub, gw_pub)))
    final = json.loads((await read_plain_frame(reader)).decode())
    assert final.get("ok") is True, final
    writer.close()
    await writer.wait_closed()
    return dev_priv, dev_pub, gw_pub


async def open_session(tcp_port: int, priv: bytes, dev_pub: bytes, gw_pub: bytes):
    reader, writer = await asyncio.open_connection("127.0.0.1", tcp_port)
    writer.write(bytes([MODE_SESSION]) + device_tag(dev_pub))  # tag 由公钥派生
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


async def hello(conn, writer, reader, name: str) -> dict:
    await write_message(
        writer, conn,
        json.dumps({
            "type": "hello", "id": "h-1",
            "payload": {
                "proto_version": 1, "device_id": name, "toolset_version": 1,
                "device": {"model": "test", "android": 33, "app_version": "0.1.0"},
            },
        }).encode(),
    )
    return json.loads((await read_message(reader, conn)).decode())


@pytest.mark.asyncio
async def test_rename_after_pairing_keeps_connection(tmp_path):
    """核心场景：配对为 mi14 → 设备改名 my-phone → 仍能连上且标签更新。"""
    async with running_gateway(tmp_path) as (http_port, tcp_port):
        async with mcp_client(http_port) as session:
            gen = await session.call_tool("generate_pairing_code", {"device_name": "mi14"})
            code = gen.structured_content["code"]

        dev_priv, dev_pub, gw_pub = await pair(tcp_port, code, "mi14")

        # 用新名字连接（tag 是公钥派生的，与名字无关）
        conn, reader, writer = await open_session(tcp_port, dev_priv, dev_pub, gw_pub)
        ack = await hello(conn, writer, reader, "my-phone")
        assert ack["type"] == "hello_ack", ack
        print("  ✓ 改名后连接成功")

        # MCP 侧看到的是新名字，旧名字消失
        async with mcp_client(http_port) as session:
            body = (await session.call_tool("list_paired_devices", {})).structured_content
            names = [d["device"] for d in body["paired"]]
            assert names == ["my-phone"], names
            assert body["count"] == 1, "改名不应产生额外条目"
            assert body["paired"][0]["renamed_at"] is not None
            print(f"  ✓ 配对记录迁移为 {names}，仅 1 条")

            # 也能正常调用工具（在线名单用新名）
            online = (await session.call_tool("list_devices", {})).structured_content["online"]
            assert online == ["my-phone"]

        writer.close()


@pytest.mark.asyncio
async def test_rename_to_other_devices_name_rejected(tmp_path):
    """改名撞到另一台已配对设备的名字 → 拒绝。"""
    async with running_gateway(tmp_path) as (http_port, tcp_port):
        async with mcp_client(http_port) as session:
            code_a = (await session.call_tool("generate_pairing_code", {"device_name": "dev-a"})).structured_content["code"]
        priv_a, pub_a, gw_pub = await pair(tcp_port, code_a, "dev-a")

        async with mcp_client(http_port) as session:
            code_b = (await session.call_tool("generate_pairing_code", {"device_name": "dev-b"})).structured_content["code"]
        priv_b, pub_b, _ = await pair(tcp_port, code_b, "dev-b")

        conn, reader, writer = await open_session(tcp_port, priv_b, pub_b, gw_pub)
        err = await hello(conn, writer, reader, "dev-a")
        assert err["type"] == "error" and err["payload"]["code"] == "NAME_IN_USE"
        print(f"  ✓ 改名撞车被拒: {err['payload']['message']}")

        # 两台设备的标签都未被篡改
        async with mcp_client(http_port) as session:
            body = (await session.call_tool("list_paired_devices", {})).structured_content
            assert {d["device"] for d in body["paired"]} == {"dev-a", "dev-b"}
        writer.close()


@pytest.mark.asyncio
async def test_unpair_revokes_access(tmp_path):
    """吊销后设备无法再建立会话，且在线会话被立即断开。"""
    async with running_gateway(tmp_path) as (http_port, tcp_port):
        async with mcp_client(http_port) as session:
            code = (await session.call_tool("generate_pairing_code", {"device_name": "mi14"})).structured_content["code"]
        dev_priv, dev_pub, gw_pub = await pair(tcp_port, code, "mi14")

        # 建立在线会话
        conn, reader, writer = await open_session(tcp_port, dev_priv, dev_pub, gw_pub)
        assert (await hello(conn, writer, reader, "mi14"))["type"] == "hello_ack"

        async with mcp_client(http_port) as session:
            body = (await session.call_tool("list_paired_devices", {})).structured_content
            assert body["paired"][0]["online"] is True

            r = await session.call_tool("unpair_device", {"target": "mi14"})
            assert r.structured_content["unpaired"] == "mi14"
            assert r.structured_content["disconnected"] is True
            print("  ✓ 吊销成功，在线会话已断开")

            body = (await session.call_tool("list_paired_devices", {})).structured_content
            assert body["count"] == 0

        # 再连 → 被拒。公钥已删，tag 只剩吊销墓碑：网关回一个**明确的拒绝帧**
        # （而不是静默断开），设备才能区分"被吊销"与"网络抖动"、停机等人工重配对。
        r2, w2 = await asyncio.open_connection("127.0.0.1", tcp_port)
        w2.write(bytes([MODE_SESSION]) + device_tag(dev_pub))
        await w2.drain()
        flags, payload = await asyncio.wait_for(read_plain_frame_ex(r2), 3)
        assert flags & FLAG_REJECT, f"吊销后应收到拒绝帧，实际 flags={flags}"
        assert json.loads(payload.decode())["code"] == REJECT_DEVICE_REVOKED
        assert await asyncio.wait_for(r2.read(1), 3) == b"", "拒绝帧之后应断开"
        w2.close()
        print("  ✓ 吊销后重连收到 DEVICE_REVOKED 拒绝帧（设备据此停机）")

        async with mcp_client(http_port) as session:
            r = await session.call_tool("unpair_device", {"target": "mi14"})
            assert r.is_error and "DEVICE_NOT_FOUND" in r.content[0].text
        writer.close()


@pytest.mark.asyncio
async def test_repair_same_key_does_not_duplicate(tmp_path):
    """同设备改名重配对 → 迁移条目而非新增。"""
    async with running_gateway(tmp_path) as (http_port, tcp_port):
        async with mcp_client(http_port) as session:
            code1 = (await session.call_tool("generate_pairing_code", {"device_name": "old"})).structured_content["code"]
            dev_priv, dev_pub, gw_pub = await pair(tcp_port, code1, "old")

            code2 = (await session.call_tool("generate_pairing_code", {"device_name": "new"})).structured_content["code"]

            # 用**同一密钥对**重新配对为 new
            reader, writer = await asyncio.open_connection("127.0.0.1", tcp_port)
            writer.write(bytes([MODE_ENROLL]))
            await write_plain_frame(
                writer, json.dumps({"name": "new", "dev_pub": base64.b64encode(dev_pub).decode()}).encode()
            )
            ch = json.loads((await read_plain_frame(reader)).decode())
            nonce = base64.b64decode(ch["nonce"])
            gw = base64.b64decode(ch["gw_pub"])
            key = derive_key(code2, pairing_salt("new", gw))
            await write_plain_frame(writer, base64.b64encode(compute_proof(key, "dev", nonce, "new", dev_pub, gw)))
            assert json.loads((await read_plain_frame(reader)).decode())["ok"] is True
            writer.close()

            body = (await session.call_tool("list_paired_devices", {})).structured_content
            assert body["count"] == 1, f"应为 1 条，实际: {body}"
            assert body["paired"][0]["device"] == "new"
            print("  ✓ 同密钥重配对 → 条目迁移，无重复")


@pytest.mark.asyncio
async def test_pair_name_taken_by_other_device_rejected(tmp_path):
    """配对时名字已被另一台占用 → 明确拒绝。"""
    async with running_gateway(tmp_path) as (http_port, tcp_port):
        async with mcp_client(http_port) as session:
            c1 = (await session.call_tool("generate_pairing_code", {"device_name": "taken"})).structured_content["code"]
        await pair(tcp_port, c1, "taken")

        async with mcp_client(http_port) as session:
            c2 = (await session.call_tool("generate_pairing_code", {"device_name": "taken"})).structured_content["code"]

        reader, writer = await asyncio.open_connection("127.0.0.1", tcp_port)
        writer.write(bytes([MODE_ENROLL]))
        _, other_pub = gen_keypair()
        await write_plain_frame(
            writer, json.dumps({"name": "taken", "dev_pub": base64.b64encode(other_pub).decode()}).encode()
        )
        ch = json.loads((await read_plain_frame(reader)).decode())
        nonce = base64.b64decode(ch["nonce"])
        gw = base64.b64decode(ch["gw_pub"])
        key = derive_key(c2, pairing_salt("taken", gw))
        await write_plain_frame(writer, base64.b64encode(compute_proof(key, "dev", nonce, "taken", other_pub, gw)))
        final = json.loads((await read_plain_frame(reader)).decode())
        assert final.get("ok") is False
        print(f"  ✓ 配对撞名被拒: {final.get('err', '')[:40]}")
        writer.close()
