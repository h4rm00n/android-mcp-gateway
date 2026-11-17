"""设备会话注册表：device_id → 在线会话，及在途 tool_call 的 Future。"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from starlette.websockets import WebSocket


@dataclass
class DeviceSession:
    device_id: str
    ws: WebSocket
    session_id: str
    heartbeat_interval_ms: int
    pending: dict[str, asyncio.Future] = field(default_factory=dict)
    last_seen: float = field(default_factory=time.monotonic)
    # hello 自报的设备信息（型号/安卓版本/App 版本），供 list_devices 展示
    model: str = ""
    android: int = 0
    app_version: str = ""

    def touch(self) -> None:
        self.last_seen = time.monotonic()


@dataclass
class DeviceHistory:
    """离线设备的历史痕迹——AI 靠它区分"从没连过"与"连过但当前离线"。"""

    model: str = ""
    android: int = 0
    app_version: str = ""
    last_online_ts: int = 0  # epoch ms
    last_offline_ts: int = 0  # epoch ms


class DeviceRegistry:
    def __init__(self) -> None:
        self._sessions: dict[str, DeviceSession] = {}
        self._history: dict[str, DeviceHistory] = {}

    def get(self, device_id: str) -> DeviceSession | None:
        return self._sessions.get(device_id)

    def put(self, session: DeviceSession) -> DeviceSession | None:
        """注册新会话，返回被顶替的旧会话（协议 §8：新连接顶替旧连接）。"""
        old = self._sessions.get(session.device_id)
        self._sessions[session.device_id] = session
        hist = self._history.setdefault(session.device_id, DeviceHistory())
        hist.model = session.model or hist.model
        hist.android = session.android or hist.android
        hist.app_version = session.app_version or hist.app_version
        hist.last_online_ts = int(time.time() * 1000)
        return old

    def pop(self, device_id: str, session: DeviceSession) -> None:
        """移除会话——仅当注册表里仍是它自己（顶替场景下旧连接清理不能误删新连接）。"""
        if self._sessions.get(device_id) is session:
            self._sessions.pop(device_id, None)
            hist = self._history.setdefault(device_id, DeviceHistory())
            hist.last_offline_ts = int(time.time() * 1000)

    def online_names(self) -> list[str]:
        return sorted(self._sessions)

    def snapshot(self) -> list[dict]:
        """全部已知设备（在线 + 有历史记录的离线）的统一视图。"""
        now = int(time.time() * 1000)
        names = sorted(set(self._sessions) | set(self._history))
        out: list[dict] = []
        for name in names:
            session = self._sessions.get(name)
            hist = self._history.get(name, DeviceHistory())
            if session is not None:
                silent_ms = int((time.monotonic() - session.last_seen) * 1000)
                out.append(
                    {
                        "device": name,
                        "online": True,
                        "model": session.model or hist.model,
                        "android": session.android or hist.android,
                        "app_version": session.app_version or hist.app_version,
                        "silent_ms": silent_ms,
                    }
                )
            else:
                out.append(
                    {
                        "device": name,
                        "online": False,
                        "model": hist.model,
                        "android": hist.android,
                        "app_version": hist.app_version,
                        "offline_for_s": (
                            int((now - hist.last_offline_ts) / 1000) if hist.last_offline_ts else None
                        ),
                        "last_online_ts": hist.last_online_ts or None,
                    }
                )
        return out
