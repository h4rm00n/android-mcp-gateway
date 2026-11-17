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
    FLAG_REJECT,
    MODE_ENROLL,
    MODE_SESSION,
    NOISE_CHUNK,
    PATTERN,
    REJECT_DEVICE_REVOKED,
    TAG_LEN,
    read_message,
    read_plain_frame,
    read_plain_frame_ex,
    write_message,
    write_plain_frame,
)
from gateway.pairing import PairingManager, compute_proof, derive_key, normalize
from gateway.ratelimit import FAIL_LIMIT, AuthRateLimiter
from gateway.registry import DeviceRegistry
from gateway.keystore import device_tag
from gateway.tcp_server import start_tcp_server


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
    """执行配对流程，返回 (设备私钥, 设备公钥)。每次生成新密钥。"""
    return await enroll_with_key(server, code, gen_keypair(), name, expect_ok=expect_ok)


async def enroll_with_key(
    server: _TestServer,
    code: str,
    keypair: tuple[bytes, bytes],
    name: str = "mi14",
    *,
    expect_ok: bool = True,
) -> tuple[bytes, bytes]:
    """用**指定的设备密钥**执行配对流程。

    真实 App 用 `DeviceKeys.loadOrCreate()` 复用同一把密钥跨多次配对
    （重新配对不会换密钥），所以"重新配对是否解除吊销"必须用同一把密钥验证。
    """
    dev_priv, dev_pub = keypair
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
    """建立 Noise_KK 会话，返回 (conn, reader, writer)。tag 由**公钥**派生。"""
    reader, writer = await server.open()
    writer.write(bytes([MODE_SESSION]) + device_tag(pub))
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
    writer.write(bytes([MODE_SESSION]) + device_tag(dev_pub))  # 未登记的公钥
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
async def test_session_rename_is_allowed_and_persisted(server):
    """身份=公钥，名字只是标签：改名后仍能连上，keystore 标签随之更新。"""
    code = server.pairing.issue("mi14")
    dev_priv, dev_pub = await enroll_client(server, code, name="mi14")
    assert server.store.device("mi14") is not None

    # 用**同一个公钥**的 tag 连接（改名不影响 tag），但 hello 里报新名字
    conn, reader, writer = await noise_session(server, dev_priv, dev_pub)
    await send_enc(conn, writer, hello_payload(name="my-new-name"))
    ack = await recv_enc(conn, reader)
    assert ack["type"] == "hello_ack", f"改名后应正常注册: {ack}"

    # 在线身份用新名
    assert server.registry.get("my-new-name") is not None
    # keystore 标签已迁移（旧名不再存在，且不是新增一条）
    assert server.store.device("mi14") is None
    renamed = server.store.device("my-new-name")
    assert renamed is not None and renamed.public_key == dev_pub
    assert len(server.store.devices()) == 1, "改名不应产生额外条目"
    writer.close()


@pytest.mark.asyncio
async def test_session_rename_to_taken_name_rejected(server):
    """已配对的另一台设备占用了目标名字 → 改名被拒（防冒名顶替在线身份）。"""
    # 配对两台设备
    code_a = server.pairing.issue("dev-a")
    priv_a, pub_a = await enroll_client(server, code_a, name="dev-a")
    code_b = server.pairing.issue("dev-b")
    priv_b, pub_b = await enroll_client(server, code_b, name="dev-b")

    # dev-b 试图改名为 dev-a
    conn, reader, writer = await noise_session(server, priv_b, pub_b)
    await send_enc(conn, writer, hello_payload(name="dev-a"))
    err = await recv_enc(conn, reader)
    assert err["type"] == "error"
    assert err["payload"]["code"] == "NAME_IN_USE"
    # 标签未被篡改
    assert server.store.device("dev-a").public_key == pub_a
    assert server.store.device("dev-b").public_key == pub_b
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


# ---------------------------------------------------------------- 吊销


@pytest.mark.asyncio
async def test_revoked_device_gets_explicit_reject_frame(server):
    """吊销后重连：必须收到 DEVICE_REVOKED 拒绝帧，而不是只有 EOF。

    设备侧靠这个帧区分"被吊销"与"网络抖动"——收到它就停机等人工重新配对，
    而不是以退避间隔永远重试。
    """
    code = server.pairing.issue("mi14")
    dev_priv, dev_pub = await enroll_client(server, code)

    # 先正常连一次，确认配对有效
    conn, r1, w1 = await noise_session(server, dev_priv, dev_pub)
    await send_enc(conn, w1, hello_payload())
    await recv_enc(conn, r1)
    w1.close()
    await asyncio.sleep(0.05)  # 等服务端会话清理，避免与下面的新连接抢 registry

    # 网关侧吊销
    assert server.store.unpair("mi14") is True
    assert server.store.device("mi14") is None
    assert server.store.is_revoked(device_tag(dev_pub)), "吊销必须留墓碑"

    # 同一把密钥重连：读到拒绝帧（flags bit1），内容指明原因
    reader, writer = await server.open()
    writer.write(bytes([MODE_SESSION]) + device_tag(dev_pub))
    await writer.drain()
    flags, payload = await asyncio.wait_for(read_plain_frame_ex(reader), 5)
    assert flags & FLAG_REJECT, f"应带拒绝标记，实际 flags={flags}"
    body = json.loads(payload.decode())
    assert body["code"] == REJECT_DEVICE_REVOKED
    # 拒绝帧之后连接关闭
    assert await asyncio.wait_for(reader.read(1), 5) == b""
    writer.close()


@pytest.mark.asyncio
async def test_never_paired_tag_gets_silent_close(server):
    """从未配对过的 tag：静默断开，不回拒绝帧（不向未认证对端确认任何信息）。"""
    _, dev_pub = gen_keypair()
    reader, writer = await server.open()
    writer.write(bytes([MODE_SESSION]) + device_tag(dev_pub))
    await writer.drain()
    # 没有任何拒绝帧，直接 EOF
    assert await asyncio.wait_for(reader.read(1), 5) == b""
    writer.close()


@pytest.mark.asyncio
async def test_repair_after_revoke_clears_tombstone(server):
    """吊销后用新配对码重新配对 → 墓碑清除，连接恢复正常。"""
    code = server.pairing.issue("mi14")
    dev_priv, dev_pub = await enroll_client(server, code)
    server.store.unpair("mi14")
    assert server.store.is_revoked(device_tag(dev_pub))

    # 重新配对（同一把设备密钥，用户重新索要配对码 = 明确授权）
    code2 = server.pairing.issue("mi14")
    dev_priv, dev_pub = await enroll_client(server, code2)
    assert not server.store.is_revoked(device_tag(dev_pub)), "重新配对应清除墓碑"

    conn, reader, writer = await noise_session(server, dev_priv, dev_pub)
    await send_enc(conn, writer, hello_payload())
    ack = await recv_enc(conn, reader)
    assert ack["type"] == "hello_ack"
    writer.close()


@pytest.mark.asyncio
async def test_tombstone_survives_restart(tmp_path):
    """墓碑必须落盘：网关重启后仍能对已吊销设备给出明确原因。"""
    store_path = tmp_path / "gw.keys"
    store = KeyStore(store_path)
    _, pub = gen_keypair()
    store.pair("mi14", pub)
    store.unpair("mi14")

    reloaded = KeyStore(store_path)
    assert reloaded.is_revoked(device_tag(pub))
    assert reloaded.device("mi14") is None


# ---------------------------------------------------------------- 墓碑重复进/出


@pytest.mark.asyncio
async def test_repeated_pair_revoke_cycles_stay_consistent(server):
    """反复「配对 → 吊销」多轮（同一把设备密钥）：状态必须每轮都干净。

    真实 App 的 `DeviceKeys.loadOrCreate()` 跨多次配对复用同一把密钥，
    所以要按同一密钥反复进出验证。墓碑是 set，重复写入不会膨胀；
    更关键的是每轮 unpair 后 is_revoked 必须为真、pair 后必须为假——
    否则第二轮起设备就收不到拒绝帧了。
    """
    kp = gen_keypair()
    code = server.pairing.issue("mi14")
    _, dev_pub = await enroll_with_key(server, code, kp)
    tag = device_tag(dev_pub)
    assert tag == device_tag(kp[1])

    for round_no in range(3):
        assert server.store.device("mi14") is not None, f"第 {round_no + 1} 轮应处于已配对"
        assert not server.store.is_revoked(tag)

        assert server.store.unpair("mi14") is True
        assert server.store.is_revoked(tag), f"第 {round_no + 1} 轮吊销后应有墓碑"

        # 用同一把密钥重新配对（App 行为：重新配对不换密钥）
        code = server.pairing.issue("mi14")
        await enroll_with_key(server, code, kp)
        assert not server.store.is_revoked(tag), f"第 {round_no + 1} 轮重配对后墓碑应清除"

        # 墓碑集合不因反复进出而膨胀
        raw = json.loads(server.store._path.read_text(encoding="utf-8"))
        assert raw["revoked_tags"] == [], f"当前无吊销，墓碑应为空: {raw['revoked_tags']}"


@pytest.mark.asyncio
async def test_repeated_unpair_is_idempotent(server):
    """对已吊销（记录已删）的设备再次吊销：返回 False，不改动墓碑状态。"""
    code = server.pairing.issue("mi14")
    _, dev_pub = await enroll_client(server, code)
    tag = device_tag(dev_pub)

    assert server.store.unpair("mi14") is True
    assert server.store.is_revoked(tag)

    # 再删一次：没有记录可删，返回 False，但墓碑不该丢（并发/重试都可能撞上）
    assert server.store.unpair("mi14") is False
    assert server.store.is_revoked(tag), "重复吊销不应清掉已有墓碑"

    assert server.store.unpair_by_public_key(dev_pub) is None
    assert server.store.is_revoked(tag)


@pytest.mark.asyncio
async def test_revoked_reconnect_never_triggers_ip_ban(server):
    """被吊销设备反复重连 **不应** 把 IP 打进限速封禁。

    若不记失败：第 N 次仍能收到拒绝帧。
    记失败（旧实现）的话第 5 次触发封禁，第 6 次起连拒绝帧都发不出——
    设备只看到 EOF，NAT 后面的其它正常设备也被一起封 10 分钟。
    """
    code = server.pairing.issue("mi14")
    _, dev_pub = await enroll_client(server, code)
    tag = device_tag(dev_pub)
    server.store.unpair("mi14")

    for attempt in range(1, 8):  # 远超 5 次失败阈值
        reader, writer = await server.open()
        writer.write(bytes([MODE_SESSION]) + tag)
        await writer.drain()
        flags, payload = await asyncio.wait_for(read_plain_frame_ex(reader), 5)
        assert flags & FLAG_REJECT, f"第 {attempt} 次重连应仍收到拒绝帧"
        assert json.loads(payload.decode())["code"] == REJECT_DEVICE_REVOKED
        writer.close()
        await asyncio.sleep(0.01)

    assert not server.limiter.banned("127.0.0.1"), "吊销重连不计入限速，IP 不应被封"
    assert not server.pairing.is_blocked("127.0.0.1")


@pytest.mark.asyncio
async def test_unknown_tag_still_rate_limited(server):
    """对比项：从未配对过的 tag 才该计入限速（tag 空间可枚举，是真正的探测面）。"""
    _, pub = gen_keypair()
    tag = device_tag(pub)

    for _ in range(FAIL_LIMIT):
        reader, writer = await server.open()
        writer.write(bytes([MODE_SESSION]) + tag)
        await writer.drain()
        assert await asyncio.wait_for(reader.read(1), 5) == b""
        writer.close()

    assert server.limiter.banned("127.0.0.1"), "未知 tag 探测 5 次后应封禁"


@pytest.mark.asyncio
async def test_tombstone_accumulates_across_keys_and_name_reuse(tmp_path):
    """同名换密钥：旧密钥的墓碑保留，新密钥不受影响（判定按公钥不按名字）。"""
    store = KeyStore(tmp_path / "gw.keys")

    _, pub_a = gen_keypair()
    store.pair("shared", pub_a)
    store.unpair("shared")
    assert store.is_revoked(device_tag(pub_a))

    # 新设备接管同一个名字
    _, pub_b = gen_keypair()
    store.pair("shared", pub_b)
    assert not store.is_revoked(device_tag(pub_b)), "新设备不应被判为已吊销"
    assert store.is_revoked(device_tag(pub_a)), "旧密钥墓碑应保留（它确实被吊销了）"

    # 第二把密钥也被吊销 → 两条墓碑并存（不同公钥，预期累积而非去重）
    store.unpair("shared")
    raw = json.loads((tmp_path / "gw.keys").read_text(encoding="utf-8"))
    assert len(raw["revoked_tags"]) == 2
