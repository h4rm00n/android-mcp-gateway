"""审计日志：JSONL，一行一事件（docs/protocol.md §11），按大小自动轮转。"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

DEFAULT_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
DEFAULT_BACKUPS = 5


class AuditLog:
    """按大小轮转的 JSONL 审计日志。

    `max_bytes`/`backups` 可配置；轮转时 audit.jsonl → audit.jsonl.1 → … → .N（丢弃最旧）。
    单用户自部署不会写爆磁盘，但长期运行（每次工具调用至少 2 行）值得这个兜底。
    """

    def __init__(
        self,
        path: str | Path,
        max_bytes: int = DEFAULT_MAX_BYTES,
        backups: int = DEFAULT_BACKUPS,
    ) -> None:
        self._path = Path(path)
        self._max_bytes = max_bytes
        self._backups = backups
        self._lock = asyncio.Lock()

    def _rotate_if_needed(self) -> None:
        try:
            if self._path.stat().st_size < self._max_bytes:
                return
        except FileNotFoundError:
            return
        # 丢弃最旧的备份
        oldest = self._path.with_suffix(self._path.suffix + f".{self._backups}")
        if oldest.exists():
            oldest.unlink()
        for i in range(self._backups - 1, 0, -1):
            src = self._path.with_suffix(self._path.suffix + f".{i}")
            if src.exists():
                src.rename(self._path.with_suffix(self._path.suffix + f".{i + 1}"))
        self._path.rename(self._path.with_suffix(self._path.suffix + ".1"))

    async def log(self, event: str, **fields: object) -> None:
        record = {"ts": int(time.time() * 1000), "event": event, **fields}
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        async with self._lock:
            self._rotate_if_needed()
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
