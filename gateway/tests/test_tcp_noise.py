"""裸 TCP(Noise) 传输端到端测试：真实 socket + 真实 Noise 握手。

覆盖：配对成功/错码/未配对拒连、KK 会话、消息往返、分块传输、MITM 检测。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import struct

import pytest
from noise.connection import Keypair, NoiseConnection

from gateway.audit import AuditLog
from gateway.keystore import KeyStore, gen_keypair
from gateway.noise_wire import (
    MODE_ENROLL,
    MODE_SESSION,
    NOISE_CHUNK,
    PATTERN,
    TAG_LEN,
    read_message,
    read_plain_frame,
    write_message,
    write_plain_frame,
)
from gateway.pairing import PairingManager, compute_proof, derive_key, normalize
from gateway.ratelimit import AuthRateLimiter
from gateway.registry import DeviceRegistry
from gateway.tcp_server import device_tag, start_tcp_server


# ---------------------------------------------------------------- 测试夹具


class _TestServer:
    def __init__(self, tmp_path) -> None:
        self.store = KeyStore(tmp_path / "gw.keys")
        self.pairing = PairingManager()
        self.registry = DeviceRegistry()
        self.limiter = AuthRateLimiter()
        self.audit = AuditLog(tmp_path / "audit.jsonl")
        self.server: asyncio.AbstractServer | None = None
        self.port = 0

    async def start(self) -> None:
        self.server = await start_tcp_server(
            "127.0.0.1", 0, self.store, self.pairing, self.registry, self.limiter, self.audit
        )
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    def open(self):
        return asyncio.open_connection("127.0.0.1", self.port)


@pytest.fixture
async def server(tmp_path):
    s = _TestServer(tmp_path)
    await s.start()
    try:
        yield s
    finally:
        await s.stop()


# ---------------------------------------------------------------- 客户端助手


async def enroll_client(
    server: _TestServer, code: str, name: str = "mi14", *, expect_ok: bool = True
) -> tuple[bytes, bytes]:
    """执行配对流程，返回 (设备私钥, 设备公钥)。"""
    dev_priv, dev_pub = gen_keypair()
    reader, writer = await server.open()
    writer.write(bytes([MODE_ENROLL]))
    await write_plain_frame(
        writer,
        json.dumps({"name": name, "dev_pub": base64.b64encode(dev_pub).decode()}).encode(),
    )
    resp = json.loads((await read_plain_frame(reader)).decode())
    nonce = base64.b64decode(resp["nonce"])
    gw_pub = base64.b64decode(resp["gw_pub"])

    key = derive_key(code, _pairing_salt(server, name))
    proof = compute_proof(key, "dev", nonce, name, dev_pub, gw_pub)
    await write_plain_frame(writer, base64.b64encode(proof))

    final = json.loads((await read_plain_frame(reader)).decode())
    if not expect_ok:
        assert final.get("ok") is False
        writer.close()
        return dev_priv, dev_pub

    assert final.get("ok") is True, final
    # 反向证明必须能验通（MTM 检测的核心）
    expected = compute_proof(key, "gw", nonce, name, dev_pub, gw_pub)
    assert base64.b64decode(final["proof"]) == expected, "网关反向证明无效"
    writer.close()
    await writer.wait_closed()
    return dev_priv, dev_pub


def _pairing_salt(server: _TestServer, name: str) -> bytes:
    """确定性盐：设备名 + 网关公钥（生产与测试一致，见 pairing.pairing_salt）。"""
    from gateway.pairing import pairing_salt
    return pairing_salt(name, server.store.gateway_public)


async def noise_session(server: _TestServer, priv: bytes, pub: bytes, name: str = "mi14"):
    """建立 Noise_KK 会话，返回 (conn, reader, writer)。"""
    reader, writer = await server.open()
    writer.write(bytes([MODE_SESSION]) + device_tag(name))
    await writer.drain()

    conn = NoiseConnection.from_name(PATTERN)
    conn.set_as_initiator()
    conn.set_keypair_from_private_bytes(Keypair.STATIC, priv)
    conn.set_keypair_from_public_bytes(Keypair.REMOTE_STATIC, server.store.gateway_public)
    conn.start_handshake()
    await write_plain_frame(writer, conn.write_message())
    conn.read_message(await read_plain_frame(reader))
    assert conn.handshake_finished
    return conn, reader, writer


async def send_enc(conn, writer, obj: dict) -> None:
    await write_message(writer, conn, json.dumps(obj).encode())


async def recv_enc(conn, reader) -> dict:
    return json.loads((await read_message(reader, conn)).decode())


def hello_payload(name: str = "mi14") -> dict:
    return {
        "type": "hello",
        "id": "h-1",
        "payload": {
            "proto_version": 1,
            "device_id": name,
            "toolset_version": 1,
            "device": {"model": "test", "android": 33, "app_version": "0.1.0"},
        },
    }


# ---------------------------------------------------------------- 配对


@pytest.mark.asyncio
async def test_enroll_success(server):
    code = server.pairing.issue("mi14")
    dev_priv, dev_pub = await enroll_client(server, code)
    assert server.store.device("mi14") is not None
    assert server.store.device("mi14").public_key == dev_pub
    # 单次有效：码已作废
    assert server.pairing.pending_count() == 0


@pytest.mark.asyncio
async def test_enroll_wrong_code_rejected(server):
    server.pairing.issue("mi14")
    await enroll_client(server, "WRONGCOD", expect_ok=False)
    assert server.store.device("mi14") is None


@pytest.mark.asyncio
async def test_enroll_no_code_rejected(server):
    await enroll_client(server, "ANYTHING", expect_ok=False)
    assert server.store.device("mi14") is None


@pytest.mark.asyncio
async def test_enroll_mitm_cannot_swap_gateway_key(server):
    """攻击者把自己的公钥塞给设备 → 设备的 proof 里绑定了错误 gw_pub → 网关验签失败。"""
    code = server.pairing.issue("mi14")
    dev_priv, dev_pub = gen_keypair()
    reader, writer = await server.open()
    writer.write(bytes([MODE_ENROLL]))
    await write_plain_frame(
        writer, json.dumps({"name": "mi14", "dev_pub": base64.b64encode(dev_pub).decode()}).encode()
    )
    resp = json.loads((await read_plain_frame(reader)).decode())
    nonce = base64.b64decode(resp["nonce"])

    # 中间人替换 gateway 公钥（设备实际拿到的会是攻击者的公钥）
    fake_gw_pub = b"\xAA" * 32
    key = derive_key(code, _pairing_salt(server, "mi14"))
    proof = compute_proof(key, "dev", nonce, "mi14", dev_pub, fake_gw_pub)
    await write_plain_frame(writer, base64.b64encode(proof))

    final = json.loads((await read_plain_frame(reader)).decode())
    assert final.get("ok") is False, "网关不应接受针对伪造公钥的证明"
    writer.close()


@pytest.mark.asyncio
async def test_enroll_rate_limited(server):
    """连续 5 次错码后，同 IP 即使拿正确码也应被拒（防 PBKDF2 CPU DoS）。"""
    server.pairing.issue("mi14")
    for _ in range(5):
        try:
            await enroll_client(server, "BADCODE1", expect_ok=False)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass  # 限速生效后服务端直接断开，不再回帧

    assert server.pairing.is_blocked("127.0.0.1")

    # 新码也应被阻断（限速发生在验码之前）
    server.pairing.issue("mi14")
    with pytest.raises((asyncio.IncompleteReadError, ConnectionError)):
        await enroll_client(server, "GOODCODE", expect_ok=False)
    assert server.store.device("mi14") is None


# ---------------------------------------------------------------- 会话


@pytest.mark.asyncio
async def test_session_requires_pairing(server):
    """未配对设备的 tag 查不到 → 直接断开（不进入握手）。"""
    _, dev_pub = gen_keypair()
    reader, writer = await server.open()
    writer.write(bytes([MODE_SESSION]) + device_tag("ghost"))
    await writer.drain()
    data = await asyncio.wait_for(reader.read(1), 5)
    assert data == b""  # 对端已关闭


@pytest.mark.asyncio
async def test_session_hello_and_heartbeat(server):
    code = server.pairing.issue("mi14")
    dev_priv, _ = await enroll_client(server, code)

    conn, reader, writer = await noise_session(server, dev_priv, _)
    await send_enc(conn, writer, hello_payload())
    ack = await recv_enc(conn, reader)
    assert ack["type"] == "hello_ack"
    assert ack["payload"]["heartbeat_interval_ms"] == 30_000

    await send_enc(conn, writer, {"type": "heartbeat", "id": "hb-1", "payload": {}})
    hb = await recv_enc(conn, reader)
    assert hb["type"] == "heartbeat_ack"

    # 设备应出现在注册表里
    assert server.registry.get("mi14") is not None

    writer.close()


@pytest.mark.asyncio
async def test_session_large_message_chunking(server):
    """payload 超过 Noise 单帧上限时必须分块传输且能完整还原。"""
    code = server.pairing.issue("mi14")
    dev_priv, dev_pub = await enroll_client(server, code)
    conn, reader, writer = await noise_session(server, dev_priv, dev_pub)

    await send_enc(conn, writer, hello_payload())
    await recv_enc(conn, reader)  # hello_ack

    # 构造一个明显大于 NOISE_CHUNK 的 tool_result
    big = "x" * (NOISE_CHUNK * 2 + 1234)
    call_id = "c-big"
    # 先让网关有一个在途调用：直接塞一个 future 进注册表
    session = server.registry.get("mi14")
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    session.pending[call_id] = fut

    await send_enc(
        conn, writer,
        {"type": "tool_result", "id": call_id, "payload": {"ok": True, "result": {"data": big}}},
    )
    result = await asyncio.wait_for(fut, 10)
    assert result["result"]["data"] == big
    writer.close()


@pytest.mark.asyncio
async def test_session_identity_bound_to_pairing(server):
    """hello 自报的名字与配对身份不符 → 拒绝（防已配对设备冒充他人）。"""
    code = server.pairing.issue("mi14")
    dev_priv, dev_pub = await enroll_client(server, code)
    conn, reader, writer = await noise_session(server, dev_priv, dev_pub)

    await send_enc(conn, writer, hello_payload(name="other-device"))
    err = await recv_enc(conn, reader)
    assert err["type"] == "error"
    assert "不符" in err["payload"]["message"]
    writer.close()


@pytest.mark.asyncio
async def test_session_tampered_ciphertext_fails(server):
    """篡改密文必须导致解密失败（AEAD 完整性）。"""
    code = server.pairing.issue("mi14")
    dev_priv, dev_pub = await enroll_client(server, code)
    conn, reader, writer = await noise_session(server, dev_priv, dev_pub)

    ct = bytearray(conn.encrypt(b'{"type":"heartbeat","id":"x","payload":{}}'))
    ct[-1] ^= 0xFF
    header = struct.pack(">IB", len(ct), 1)
    writer.write(header + bytes(ct))
    await writer.drain()

    # 网关解密失败 → 关闭连接
    data = await asyncio.wait_for(reader.read(1), 5)
    assert data == b""
    writer.close()
