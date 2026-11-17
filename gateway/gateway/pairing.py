"""配对码：生成与校验。

安全设计（详见 docs/raw-tcp-noise.md）：

- **40 bit 熵**：8 字符 Crockford Base32（去掉 I/L/O/U，避免手输歧义），显示为 `K7M2-P9XR`
- **PBKDF2-HMAC-SHA256 300k 迭代加固**：把单次尝试成本抬到 ~40ms，
  配合 5 分钟 TTL，离线爆破需要百万级 GPU 并发才可能在上百秒窗口内完成
- **码只作 HMAC 密钥，永不出现在网络报文中**（proof = HMAC(K, role‖nonce‖name‖双方公钥)）
- **单次有效**：配对成功立即作废
- **失败限速**：同 IP 连续失败 5 次 → 10 分钟内拒绝再次配对（防 CPU 耗尽型 DoS）
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field

# Crockford Base32：0-9 + A-Z 去掉 I L O U
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LEN = 8  # 8 × 5 bit = 40 bit
PBKDF2_ITERS = 300_000
TTL_SECONDS = 300
MAX_PENDING = 4  # 同时存在的未用配对码上限
MAX_FAILED_ATTEMPTS = 5
FAIL_WINDOW_SECONDS = 600

# 手输容错：O→0、I/L→1（Crockford 规范）
_ALIAS = str.maketrans({"O": "0", "I": "1", "L": "1"})


def normalize(raw: str) -> str:
    """去除分隔符/大小写/易混字符后的规范形式。"""
    cleaned = "".join(ch for ch in raw.upper() if ch.isalnum())
    return cleaned.translate(_ALIAS)


def format_code(code: str) -> str:
    """展示形式：4-4 分组，便于人眼读取与手输。"""
    return f"{code[:4]}-{code[4:]}"


def derive_key(code: str, salt: bytes) -> bytes:
    """配对码 → HMAC 密钥。PBKDF2 使离线爆破昂贵。"""
    return hashlib.pbkdf2_hmac("sha256", normalize(code).encode(), salt, PBKDF2_ITERS, dklen=32)


def pairing_salt(device_name: str, gateway_pub: bytes) -> bytes:
    """配对盐：由「设备名 + 网关静态公钥」确定性派生。

    不用随机盐，因为**设备侧必须能独立算出同一个盐**——而随机盐就得额外传一轮，
    且多码并存时无法确定用哪个盐。这里两者都在 proof 之前已知，且都不是秘密
    （设备名用户自己填，网关公钥在握手中下发）。
    """
    return hashlib.sha256(b"amg-pair-v1\x00" + device_name.encode("utf-8") + b"\x00" + gateway_pub).digest()


def compute_proof(
    key: bytes,
    role: str,
    nonce: bytes,
    device_name: str,
    device_pub: bytes,
    gateway_pub: bytes,
) -> bytes:
    """双向证明：role 区分 dev/gw，绑死 nonce 与双方公钥（换任一 → 验签失败）。"""
    msg = b"\x00".join(
        [role.encode(), nonce, device_name.encode("utf-8"), device_pub, gateway_pub]
    )
    return hmac.new(key, msg, hashlib.sha256).digest()


@dataclass
class PendingCode:
    code: str
    label: str
    created_at: float
    used: bool = False

    def expired(self, now: float, ttl: int = TTL_SECONDS) -> bool:
        return now - self.created_at > ttl


@dataclass
class _FailRecord:
    count: int = 0
    first_at: float = field(default_factory=time.monotonic)


class PairingManager:
    """配对码生命周期管理。单用户场景通常只有 1 个未用码。"""

    def __init__(self, ttl: int = TTL_SECONDS) -> None:
        self._ttl = ttl
        self._pending: dict[str, PendingCode] = {}
        self._fails: dict[str, _FailRecord] = {}

    # ---- 生成 ----

    def issue(self, label: str = "") -> str:
        self._purge()
        # 超上限时先淘汰最旧的，保证可重复生成而不无限堆积
        while len(self._pending) >= MAX_PENDING:
            oldest = min(self._pending.values(), key=lambda p: p.created_at)
            self._pending.pop(oldest.code, None)
        code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))
        self._pending[code] = PendingCode(code=code, label=label, created_at=time.time())
        return code

    def pending_count(self) -> int:
        self._purge()
        return len(self._pending)

    # ---- 校验 ----

    def try_consume(self, verify_code) -> str | None:
        """遍历未过期配对码，把码本身交给 verify_code(code) 判定。

        成功即作废该码（单次有效）并返回该码；未命中返回 None。
        verify_code 内部必须用恒时比较，否则会泄露候选位置。
        """
        self._purge()
        for code, entry in list(self._pending.items()):
            if entry.used:
                continue
            if verify_code(code):
                entry.used = True
                self._pending.pop(code, None)
                return code
        return None

    def _purge(self) -> None:
        now = time.time()
        for code, entry in list(self._pending.items()):
            if entry.expired(now, self._ttl):
                self._pending.pop(code, None)

    # ---- 失败限速（防 PBKDF2 CPU DoS）----

    def is_blocked(self, ip: str) -> bool:
        rec = self._fails.get(ip)
        if rec is None:
            return False
        if time.monotonic() - rec.first_at > FAIL_WINDOW_SECONDS:
            self._fails.pop(ip, None)
            return False
        return rec.count >= MAX_FAILED_ATTEMPTS

    def note_failure(self, ip: str) -> None:
        rec = self._fails.get(ip)
        if rec is None or time.monotonic() - rec.first_at > FAIL_WINDOW_SECONDS:
            self._fails[ip] = _FailRecord(count=1, first_at=time.monotonic())
        else:
            rec.count += 1

    def note_success(self, ip: str) -> None:
        self._fails.pop(ip, None)
