"""Interop 验证：Python noiseprotocol (initiator) ↔ Java noise-java (responder)，Noise_KK。

这是裸 TCP 方案的**最高技术风险**——两个独立实现必须握手成功且能互发加密消息。
"""

import base64
import json
import os
import subprocess
import sys

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding, PrivateFormat, PublicFormat, NoEncryption,
)
from noise.connection import Keypair, NoiseConnection

PATTERN = b"Noise_KK_25519_ChaChaPoly_SHA256"


def gen_keypair() -> tuple[bytes, bytes]:
    priv = X25519PrivateKey.generate()
    priv_bytes = priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    pub_bytes = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return priv_bytes, pub_bytes


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def unb64(s: str) -> bytes:
    return base64.b64decode(s)


class JavaPeer:
    def __init__(self, java_dir: str, remote_pub: bytes, my_pub: bytes, my_priv: bytes) -> None:
        self.proc = subprocess.Popen(
            ["java", "-cp", "/tmp/noise-java.jar:" + java_dir, "NoisePeer"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
        self._send({"cmd": "init", "remote_pub": b64(remote_pub),
                    "my_pub": b64(my_pub), "my_priv": b64(my_priv)})
        resp = self._recv()
        if not resp.get("ok"):
            raise RuntimeError(f"java init failed: {resp}")

    def _send(self, obj: dict) -> None:
        assert self.proc.stdin
        self.proc.stdin.write(json.dumps(obj, separators=(",", ":")) + "\n")
        self.proc.stdin.flush()

    def _recv(self) -> dict:
        assert self.proc.stdout
        line = self.proc.stdout.readline()
        if not line:
            err = self.proc.stderr.read() if self.proc.stderr else ""
            raise RuntimeError(f"java peer died: {err}")
        return json.loads(line)

    def handshake_read(self, payload: bytes) -> bytes:
        self._send({"cmd": "read", "data": b64(payload)})
        resp = self._recv()
        if not resp.get("ok"):
            raise RuntimeError(f"java handshake failed: {resp}")
        return unb64(resp["data"]) if resp.get("data") else b""

    def split(self) -> None:
        self._send({"cmd": "split"})
        resp = self._recv()
        if not resp.get("ok"):
            raise RuntimeError(f"java split failed: {resp}")

    def send_encrypted(self, plaintext: bytes) -> bytes:
        self._send({"cmd": "write", "data": b64(plaintext)})
        return unb64(self._recv()["data"])

    def read_encrypted(self, ciphertext: bytes) -> bytes:
        self._send({"cmd": "read_enc", "data": b64(ciphertext)})
        resp = self._recv()
        if not resp.get("ok"):
            raise RuntimeError(f"java decrypt failed: {resp}")
        return unb64(resp["data"])

    def close(self) -> None:
        try:
            self._send({"cmd": "quit"})
        except Exception:
            pass
        self.proc.kill()


def main() -> None:
    init_priv, init_pub = gen_keypair()
    resp_priv, resp_pub = gen_keypair()
    print(f"initiator pub: {init_pub.hex()[:32]}...")
    print(f"responder pub: {resp_pub.hex()[:32]}...")

    java = JavaPeer("/tmp/noise-interop", init_pub, resp_pub, resp_priv)

    py = NoiseConnection.from_name(PATTERN)
    py.set_as_initiator()
    py.set_keypair_from_private_bytes(Keypair.STATIC, init_priv)
    py.set_keypair_from_public_bytes(Keypair.REMOTE_STATIC, resp_pub)
    py.start_handshake()

    # KK 只需两轮：-> e, es, ss   |   <- e, ee, se
    msg1 = py.write_message()
    msg2 = java.handshake_read(msg1)
    py.read_message(msg2)
    java.split()
    assert py.handshake_finished, "python 侧握手未完成"
    print("✓ KK 握手完成（Python initiator ↔ Java responder）")

    secret = "通知栏里有一条验证码：482913".encode()
    ct = py.encrypt(secret)
    pt = java.read_encrypted(ct)
    assert pt == secret, f"Java 解密不匹配: {pt!r}"
    print(f"✓ Python→Java 加密消息: {secret.decode()}")

    reply = "收到，已处理".encode()
    ct2 = java.send_encrypted(reply)
    pt2 = py.decrypt(ct2)
    assert pt2 == reply, f"Python 解密不匹配: {pt2!r}"
    print(f"✓ Java→Python 加密消息: {reply.decode()}")

    java.close()
    print("\n互操作验证通过 ✓ 两个独立实现可以互通")


if __name__ == "__main__":
    sys.exit(main())
