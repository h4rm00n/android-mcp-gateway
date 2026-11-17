"""认证失败限速：同 IP 连续失败 5 次 → 封 10 分钟（协议 §2，弱 token 场景的主防线）。"""

from __future__ import annotations

import time

FAIL_LIMIT = 5
BAN_SECONDS = 600


class AuthRateLimiter:
    def __init__(self, fail_limit: int = FAIL_LIMIT, ban_seconds: int = BAN_SECONDS) -> None:
        self._fail_limit = fail_limit
        self._ban_seconds = ban_seconds
        self._fails: dict[str, int] = {}
        self._banned_until: dict[str, float] = {}

    def banned(self, ip: str) -> bool:
        return self._banned_until.get(ip, 0.0) > time.monotonic()

    def record_fail(self, ip: str) -> None:
        count = self._fails.get(ip, 0) + 1
        if count >= self._fail_limit:
            self._banned_until[ip] = time.monotonic() + self._ban_seconds
            self._fails.pop(ip, None)
        else:
            self._fails[ip] = count

    def record_success(self, ip: str) -> None:
        self._fails.pop(ip, None)
