"""裸 TCP 传输：分帧 + Noise_KK 会话 + 配对协议。

线上格式（一个 TCP 连接）：

    [1 字节 mode][mode 相关载荷 ...]

    mode = 0x01  enroll（首次配对，明文 JSON 帧，走 HMAC 挑战应答）
    mode = 0x02  session（已配对，走 Noise_KK）

帧格式（mode 之后所有报文）：

    [u32 BE 总长][u8 flags][payload]
    flags: bit0=0 表示还有后续块；bit0=1 表示末块
           bit1=1 表示**拒绝帧**（握手前的明文错误，如设备已被吊销）
    payload = Noise 密文的第 k 块

Noise 单条消息明文上限 65535 字节，因此大消息分块传输（协议 §3 的 256KB 上限）。

拒绝帧（session 模式握手阶段）：

    G→D  {code:"DEVICE_REVOKED", message}   明文，flags 打 bit1

设备收到拒绝帧即知"自己被吊销了"而不是"网络抖了"，置 fatal 等人工重新配对。
只有确知被吊销的 tag 才会收到拒绝帧；从没配对过的 tag 一律静默断开。

配对握手（enroll，明文但安全——码只作 HMAC 密钥）：

    D→G  {v:1, name, dev_pub}            dev_pub 为 base64
    G→D  {nonce, gw_pub}
    D→G  {proof}                          HMAC(K, "dev"‖nonce‖name‖dev_pub‖gw_pub)
    G→D  {ok:true, proof}                 HMAC(K, "gw"‖同前缀)；ok=false 时直接断开
"""

from __future__ import annotations

import base64
import hmac
import json
import secrets
import struct
from noise.connection import Keypair, NoiseConnection

from .audit import AuditLog
from .keystore import KeyStore
from .pairing import PairingManager, compute_proof, derive_key, pairing_salt

MODE_HEADER = struct.Struct(">B")
MODE_ENROLL = 0x01
MODE_SESSION = 0x02

MAX_MESSAGE_BYTES = 256 * 1024
NOISE_CHUNK = 60000          # Noise 明文上限 65535，留余量
HEADER = struct.Struct(">IB")  # 总长 + flags
TAG_LEN = 8
HANDSHAKE_TIMEOUT_S = 15
PATTERN = b"Noise_KK_25519_ChaChaPoly_SHA256"

# 帧 flags：bit0 = 末块，bit1 = **拒绝帧**（握手前的明文错误）。
#
# 为什么需要 bit1：会话建立的第一个响应正常是 Noise msg2（随机字节）。如果网关只是
# 关掉连接，设备分不清"被吊销了"和"网络抽风了"，会当成普通断线永久重试。给拒绝帧
# 打一个显式标记位，设备就能在握手前确定性地识别，无需靠 JSON 首字节猜。
FLAG_LAST = 0x01
FLAG_REJECT = 0x02

REJECT_DEVICE_REVOKED = "DEVICE_REVOKED"


class WireError(Exception):
    """传输层错误（协议违规/握手失败/未配对）。"""


# ---------------------------------------------------------------- 分帧
#
# 关键：Noise 单条消息明文上限 65535 字节（ChaChaPoly 帧长限制），因此大消息必须
# **按明文分块、逐块加密**（每块消耗一个 nonce），而不是先整体加密再切密文。
#
# 帧格式：[u32 BE 长度][u8 flags][密文块]
#   flags bit0 = 1 表示末块


async def write_message(writer, conn, plaintext: bytes) -> None:
    """按明文分块、逐块加密后写出。"""
    if len(plaintext) > MAX_MESSAGE_BYTES:
        raise WireError(f"消息超过 {MAX_MESSAGE_BYTES} 字节上限")
    chunks = [plaintext[i:i + NOISE_CHUNK] for i in range(0, len(plaintext), NOISE_CHUNK)] or [b""]
    for idx, chunk in enumerate(chunks):
        last = 1 if idx == len(chunks) - 1 else 0
        ct = conn.encrypt(chunk)
        if len(ct) > NOISE_CHUNK + 64:
            raise WireError("密文块超限")
        writer.write(HEADER.pack(len(ct), last) + ct)
    await writer.drain()


async def read_message(reader, conn) -> bytes:
    """读到末块为止，逐块解密并拼接。"""
    buf = bytearray()
    while True:
        header = await reader.readexactly(HEADER.size)
        length, flags = HEADER.unpack(header)
        if length > NOISE_CHUNK + 64:
            raise WireError("块长度非法")
        ct = await reader.readexactly(length)
        try:
            buf.extend(conn.decrypt(ct))
        except Exception as e:  # noqa: BLE001 - 解密失败即通道不可用
            raise WireError("解密失败") from e
        if len(buf) > MAX_MESSAGE_BYTES:
            raise WireError("消息累计超限")
        if flags & 1:
            return bytes(buf)


async def read_plain_frame(reader) -> bytes:
    """读一个**未加密**帧（配对阶段使用，无 Noise 会话）。"""
    _flags, payload = await read_plain_frame_ex(reader)
    return payload


async def read_plain_frame_ex(reader) -> tuple[int, bytes]:
    """读一个未加密帧，连带返回 flags。

    设备侧区分「Noise msg2」与「拒绝帧」用的就是这个 flags：两者在线上是同一种
    分帧，只差一个比特，因此不需要额外协议回合。
    """
    header = await reader.readexactly(HEADER.size)
    length, flags = HEADER.unpack(header)
    if length > MAX_MESSAGE_BYTES:
        raise WireError("块长度非法")
    return flags, await reader.readexactly(length)


async def write_plain_frame(writer, payload: bytes) -> None:
    """写一个**未加密**帧（配对阶段使用）。"""
    if len(payload) > MAX_MESSAGE_BYTES:
        raise WireError("帧超限")
    writer.write(HEADER.pack(len(payload), FLAG_LAST) + payload)
    await writer.drain()


async def write_reject_frame(writer, code: str, message: str = "") -> None:
    """写一个**拒绝帧**：握手前的明文原因，带 FLAG_REJECT 让设备确定性识别。

    只在"我们能明确说出原因"的拒绝路径使用（设备已被吊销等）。其它情况（限速、
    tag 不存在）一律静默断开——不向未认证对端泄露"这个 tag 存在过"。
    """
    payload = json.dumps({"code": code, "message": message}).encode()
    writer.write(HEADER.pack(len(payload), FLAG_LAST | FLAG_REJECT) + payload)
    await writer.drain()


# ---------------------------------------------------------------- Noise 握手


async def server_handshake(
    reader, writer, static_private: bytes, remote_public: bytes
) -> NoiseConnection:
    """服务端 Noise_KK（responder）。KK 两轮：dev 发 e,es,ss → gw 回 e,ee,se。"""
    conn = NoiseConnection.from_name(PATTERN)
    conn.set_as_responder()
    conn.set_keypair_from_private_bytes(Keypair.STATIC, static_private)
    conn.set_keypair_from_public_bytes(Keypair.REMOTE_STATIC, remote_public)
    conn.start_handshake()
    msg1 = await read_plain_frame(reader)
    conn.read_message(msg1)
    msg2 = conn.write_message()
    await write_plain_frame(writer, msg2)
    if not conn.handshake_finished:
        raise WireError("KK 握手未完成")
    return conn


# ---------------------------------------------------------------- 配对


async def enroll(
    reader,
    writer,
    store: KeyStore,
    pairing: PairingManager,
    audit: AuditLog,
    ip: str,
) -> tuple[str, bytes]:
    """返回 (device_name, device_public_key)。失败抛 WireError（调用方负责断开）。"""
    if pairing.is_blocked(ip):
        await audit.log("enroll_reject", ip=ip, reason="rate_limited")
        raise WireError("配对尝试过于频繁")

    msg = json.loads((await read_plain_frame(reader)).decode())
    name = str(msg.get("name", "")).strip()
    try:
        dev_pub = base64.b64decode(msg["dev_pub"], validate=True)
    except Exception as e:  # noqa: BLE001
        raise WireError("dev_pub 不合法") from e
    if not name or len(dev_pub) != 32:
        raise WireError("配对参数不合法")

    nonce = secrets.token_bytes(32)
    gw_pub = store.gateway_public
    await write_plain_frame(
        writer, json.dumps({"nonce": base64.b64encode(nonce).decode(),
                            "gw_pub": base64.b64encode(gw_pub).decode()}).encode()
    )

    proof = (await read_plain_frame(reader)).decode()
    proof_bytes = base64.b64decode(proof) if proof else b""
    salt = pairing_salt(name, gw_pub)

    def verify_code(candidate: str) -> bool:
        key = derive_key(candidate, salt)
        return hmac.compare_digest(
            compute_proof(key, "dev", nonce, name, dev_pub, gw_pub), proof_bytes
        )

    matched = pairing.try_consume(verify_code)
    if matched is None:
        pairing.note_failure(ip)
        await audit.log("enroll_fail", ip=ip, device_id=name, reason="bad_code")
        # 统一失败响应，不透露"码不存在"还是"码错误"
        await write_plain_frame(writer, json.dumps({"ok": False}).encode())
        raise WireError("配对码校验失败")

    pairing.note_success(ip)
    try:
        store.pair(name, dev_pub)
    except ValueError as e:
        # 名字被另一台设备占用：明确拒绝，不覆盖
        await audit.log("enroll_fail", ip=ip, device_id=name, reason="name_taken")
        await write_plain_frame(writer, json.dumps({"ok": False, "err": str(e)}).encode())
        raise WireError(str(e)) from e
    await audit.log("paired", ip=ip, device_id=name)

    # 反向证明：设备据此确认自己没被中间人盯上（网关也持有同一个 K）
    key = derive_key(matched, salt)
    gw_proof = compute_proof(key, "gw", nonce, name, dev_pub, gw_pub)
    await write_plain_frame(
        writer,
        json.dumps({"ok": True, "proof": base64.b64encode(gw_proof).decode()}).encode(),
    )
    return name, dev_pub
