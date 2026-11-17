"""网关静态密钥 + 已配对设备公钥的持久化。

存储格式（JSON，权限应设为 0600）：

```json
{
  "gateway_private": "<base64 32B>",
  "gateway_public":  "<base64 32B>",
  "devices": {
    "mi14": {"public_key": "<base64 32B>", "paired_at": 1765000000000}
  }
}
```

网关静态密钥首次启动自动生成（X25519）。设备公钥由配对流程写入。
"""

from __future__ import annotations

import base64
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


def gen_keypair() -> tuple[bytes, bytes]:
    """生成 X25519 密钥对 → (private_raw, public_raw)。"""
    priv = X25519PrivateKey.generate()
    return (
        priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()),
        priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw),
    )


@dataclass(frozen=True)
class PairedDevice:
    name: str
    public_key: bytes
    paired_at: int


class KeyStore:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._gateway_private: bytes = b""
        self._gateway_public: bytes = b""
        self._devices: dict[str, PairedDevice] = {}
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

    # ---- 变更 ----

    def pair(self, name: str, public_key: bytes) -> PairedDevice:
        """登记/更新设备公钥（重复配对即覆盖，用于换机或轮换密钥）。"""
        if len(public_key) != 32:
            raise ValueError("X25519 公钥必须是 32 字节")
        entry = PairedDevice(name=name, public_key=public_key, paired_at=int(time.time() * 1000))
        self._devices[name] = entry
        self._save()
        return entry

    def unpair(self, name: str) -> bool:
        if self._devices.pop(name, None) is None:
            return False
        self._save()
        return True

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
            )

    def _save(self) -> None:
        payload = {
            "gateway_private": base64.b64encode(self._gateway_private).decode(),
            "gateway_public": base64.b64encode(self._gateway_public).decode(),
            "devices": {
                name: {
                    "public_key": base64.b64encode(d.public_key).decode(),
                    "paired_at": d.paired_at,
                }
                for name, d in self._devices.items()
            },
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 0600：私钥不可被他人读取
        tmp.replace(self._path)
