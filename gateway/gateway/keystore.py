"""网关静态密钥 + 已配对设备的持久化。

**身份模型（2026-11-17 修正）**：设备身份 = 静态公钥（不可变），设备名 = 可变标签。
这样设备改名字不会丢失配对（旧实现用名字做索引，一改名就断）。名字仍保持
"网关内唯一"约束，改名撞到别人已占用的名字会被拒绝。

存储格式（JSON，权限 0600）：

```json
{
  "gateway_private": "<base64 32B>",
  "gateway_public":  "<base64 32B>",
  "devices": {
    "mi14": {"public_key": "<base64 32B>", "paired_at": 1765000000000, "renamed_at": null}
  },
  "revoked_tags": ["<base64 8B>"]
}
```

`devices` 的键是当前标签（名字）。公钥才是主键——`pair()` 遇到同公钥已存在时会
迁移标签而不是新增一条，避免同一台设备留下多条陈旧记录。

`revoked_tags` 是**吊销墓碑**：只存公钥标签（SHA256 前 8 字节），不存公钥本身。
它存在的唯一目的是区分"从没配对过的 tag"与"配对过但被吊销的 tag"——后者重连时
要回一个明确的 `DEVICE_REVOKED` 拒绝帧，否则设备只会看到连接被关，当成网络问题
无限重试。重新配对同一把密钥时墓碑会被清掉。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

TAG_LEN = 8


def gen_keypair() -> tuple[bytes, bytes]:
    """生成 X25519 密钥对 → (private_raw, public_raw)。"""
    priv = X25519PrivateKey.generate()
    return (
        priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()),
        priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw),
    )


def device_tag(public_key: bytes) -> bytes:
    """公钥 → 8 字节定位标签（哈希前缀，不泄露密钥本身）。

    会话建立时设备先发此标签，网关据此查公钥——**不用名字**，因此改名不影响连接。
    """
    return hashlib.sha256(public_key).digest()[:TAG_LEN]


@dataclass(frozen=True)
class PairedDevice:
    name: str  # 当前标签（可变）
    public_key: bytes  # 真实身份（不变）
    paired_at: int
    renamed_at: int | None = None


class KeyStore:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._gateway_private: bytes = b""
        self._gateway_public: bytes = b""
        self._devices: dict[str, PairedDevice] = {}  # name → entry
        self._revoked_tags: set[bytes] = set()  # 吊销墓碑（公钥标签）
        self._load()

    # ---- 访问 ----

    @property
    def gateway_private(self) -> bytes:
        return self._gateway_private

    @property
    def gateway_public(self) -> bytes:
        return self._gateway_public

    def device(self, name: str) -> PairedDevice | None:
        return self._devices.get(name)

    def devices(self) -> list[PairedDevice]:
        return sorted(self._devices.values(), key=lambda d: d.name)

    def find_by_public_key(self, public_key: bytes) -> PairedDevice | None:
        return next((d for d in self._devices.values() if d.public_key == public_key), None)

    def find_by_tag(self, tag: bytes) -> PairedDevice | None:
        """会话建立用：按公钥标签定位设备。"""
        return next((d for d in self._devices.values() if device_tag(d.public_key) == tag), None)

    def is_revoked(self, tag: bytes) -> bool:
        """tag 是否属于**曾经配对过、之后被吊销**的设备。

        与 `find_by_tag() is None` 的区别很重要：从没配对过的 tag 必须静默断开
        （不向未认证对端确认任何信息），只有确知被吊销的才回拒绝帧。
        """
        return tag in self._revoked_tags

    def name_taken_by_other(self, name: str, public_key: bytes) -> bool:
        """名字是否已被**另一台**设备占用（同一台设备沿用自己名字不算冲突）。"""
        existing = self._devices.get(name)
        return existing is not None and existing.public_key != public_key

    # ---- 变更 ----

    def pair(self, name: str, public_key: bytes) -> PairedDevice:
        """登记设备公钥。

        关键行为：**同一公钥再次配对时迁移标签而非新增条目**（换名重配对不留垃圾记录）。
        若目标名字已被另一台设备占用，抛 ValueError（由调用方转成配对失败）。
        """
        if len(public_key) != 32:
            raise ValueError("X25519 公钥必须是 32 字节")
        if self.name_taken_by_other(name, public_key):
            raise ValueError(f"设备名 {name} 已被另一台已配对设备占用")

        now = int(time.time() * 1000)
        existing = self.find_by_public_key(public_key)
        if existing is not None:
            # 同公钥：迁移标签（可能是改名重配对）
            self._devices.pop(existing.name, None)
            entry = PairedDevice(
                name=name,
                public_key=public_key,
                paired_at=existing.paired_at,
                renamed_at=now if existing.name != name else existing.renamed_at,
            )
        else:
            entry = PairedDevice(name=name, public_key=public_key, paired_at=now)
        # 重新配对即解除吊销（用户显式给新配对码 = 明确授权）
        self._revoked_tags.discard(device_tag(public_key))
        self._devices[name] = entry
        self._save()
        return entry

    def rename(self, old_name: str, new_name: str) -> PairedDevice:
        """改名（身份=公钥，名字只是标签）。撞名抛 ValueError。"""
        entry = self._devices.get(old_name)
        if entry is None:
            raise KeyError(f"未找到已配对设备: {old_name}")
        if new_name == old_name:
            return entry
        if self.name_taken_by_other(new_name, entry.public_key):
            raise ValueError(f"设备名 {new_name} 已被另一台已配对设备占用")
        self._devices.pop(old_name, None)
        renamed = PairedDevice(
            name=new_name,
            public_key=entry.public_key,
            paired_at=entry.paired_at,
            renamed_at=int(time.time() * 1000),
        )
        self._devices[new_name] = renamed
        self._save()
        return renamed

    def unpair(self, name: str) -> bool:
        entry = self._devices.pop(name, None)
        if entry is None:
            return False
        self._revoked_tags.add(device_tag(entry.public_key))  # 留墓碑：设备重连时能收到明确原因
        self._save()
        return True

    def unpair_by_public_key(self, public_key: bytes) -> PairedDevice | None:
        entry = self.find_by_public_key(public_key)
        if entry is None:
            return None
        self._devices.pop(entry.name, None)
        self._revoked_tags.add(device_tag(entry.public_key))
        self._save()
        return entry

    # ---- 持久化 ----

    def _load(self) -> None:
        if not self._path.exists():
            priv, pub = gen_keypair()
            self._gateway_private, self._gateway_public = priv, pub
            self._save()
            return
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        self._gateway_private = base64.b64decode(raw["gateway_private"])
        self._gateway_public = base64.b64decode(raw["gateway_public"])
        for name, spec in raw.get("devices", {}).items():
            self._devices[name] = PairedDevice(
                name=name,
                public_key=base64.b64decode(spec["public_key"]),
                paired_at=int(spec.get("paired_at", 0)),
                renamed_at=spec.get("renamed_at"),
            )
        self._revoked_tags = {base64.b64decode(t) for t in raw.get("revoked_tags", [])}

    def _save(self) -> None:
        payload = {
            "gateway_private": base64.b64encode(self._gateway_private).decode(),
            "gateway_public": base64.b64encode(self._gateway_public).decode(),
            "devices": {
                name: {
                    "public_key": base64.b64encode(d.public_key).decode(),
                    "paired_at": d.paired_at,
                    "renamed_at": d.renamed_at,
                }
                for name, d in self._devices.items()
            },
            "revoked_tags": sorted(base64.b64encode(t).decode() for t in self._revoked_tags),
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 0600：私钥不可被他人读取
        tmp.replace(self._path)
