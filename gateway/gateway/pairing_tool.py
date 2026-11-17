"""配对码生成（网关本地工具，不经过设备）。

用途：用户让 AI "给 mi14 生成配对码"，AI 调用本工具拿到 8 字符码，
人眼读取后填入手机 App，手机用该码完成 HMAC 挑战应答配对（见 docs/raw-tcp-noise.md）。

安全注意：
- 码会出现在 AI 上下文里（经过 AI 服务商日志），所以 TTL 短（默认 5 分钟）+ 单次有效
- 本工具只在裸 TCP(Noise) 传输启用时有意义
"""

from __future__ import annotations

from .audit import AuditLog
from .pairing import PairingManager, format_code


def build_pairing_tool(pairing: PairingManager, audit: AuditLog, ttl_s: int):
    async def handle(args: dict) -> dict:
        label = str(args.get("device_name", "") or "").strip()
        code = pairing.issue(label)
        await audit.log("pairing_code_issued", device_id=label or None, ttl_s=ttl_s)
        return {
            "code": code,
            "display": format_code(code),
            "device_name": label or None,
            "expires_in_s": ttl_s,
            "pending_codes": pairing.pending_count(),
            "usage": (
                "把 display 里的码填到手机 App 的「配对码」输入框，点配对。"
                "码 5 分钟内有效、只能用一次。"
            ),
        }

    return handle
