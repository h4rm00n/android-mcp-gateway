"""裸 TCP 传输：分帧 + Noise_KK 会话 + 配对协议。

线上格式（一个 TCP 连接）：

    [1 字节 mode][mode 相关载荷 ...]

    mode = 0x01  enroll（首次配对，明文 JSON 帧，走 HMAC 挑战应答）
    mode = 0x02  session（已配对，走 Noise_KK）

帧格式（mode 之后所有报文）：

    [u32 BE 总长][u8 flags][payload]
    flags: bit0=0 表示还有后续块；bit0=1 表示末块
    payload = Noise 密文的第 k 块

Noise 单条消息明文上限 65535 字节，因此大消息分块传输（协议 §3 的 256KB 上限）。

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
    header = await reader.readexactly(HEADER.size)
    length, _flags = HEADER.unpack(header)
    if length > MAX_MESSAGE_BYTES:
        raise WireError("块长度非法")
    return await reader.readexactly(length)


async def write_plain_frame(writer, payload: bytes) -> None:
    """写一个**未加密**帧（配对阶段使用）。"""
    if len(payload) > MAX_MESSAGE_BYTES:
        raise WireError("帧超限")
    writer.write(HEADER.pack(len(payload), 1) + payload)
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
