"""传输通道抽象：WS 与裸 TCP(Noise) 共用的「发一帧 / 收一帧」接口。"""

from __future__ import annotations

import json
from typing import Protocol

from starlette.websockets import WebSocket, WebSocketDisconnect


class Closed(Exception):
    """对端已断开或通道不可用。"""


class Channel(Protocol):
    async def send_json(self, obj: dict) -> None: ...

    async def receive_text(self) -> str: ...

    async def close(self, code: int = 1000) -> None: ...


class WsChannel:
    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws

    async def send_json(self, obj: dict) -> None:
        try:
            await self._ws.send_json(obj)
        except (WebSocketDisconnect, RuntimeError) as e:
            raise Closed() from e

    async def send_text(self, text: str) -> None:
        try:
            await self._ws.send_text(text)
        except (WebSocketDisconnect, RuntimeError) as e:
            raise Closed() from e

    async def receive_text(self) -> str:
        try:
            return await self._ws.receive_text()
        except (WebSocketDisconnect, RuntimeError) as e:
            raise Closed() from e

    async def close(self, code: int = 1000) -> None:
        try:
            await self._ws.close(code=code)
        except Exception:  # noqa: BLE001
            pass


class NoiseChannel:
    """Noise 加密通道（分块加解密见 noise_wire.write_message/read_message）。"""

    def __init__(self, conn, reader, writer) -> None:
        self._conn = conn
        self._reader = reader
        self._writer = writer
        self._closed = False

    async def send_json(self, obj: dict) -> None:
        await self.send_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))

    async def send_text(self, text: str) -> None:
        from .noise_wire import write_message

        if self._closed:
            raise Closed()
        try:
            await write_message(self._writer, self._conn, text.encode())
        except Exception as e:  # noqa: BLE001
            raise Closed() from e

    async def receive_text(self) -> str:
        from .noise_wire import read_message

        if self._closed:
            raise Closed()
        try:
            raw = await read_message(self._reader, self._conn)
            return raw.decode()
        except Exception as e:  # noqa: BLE001 - 解密失败/超时/断连统一视为 Closed
            raise Closed() from e

    async def close(self, code: int = 1000) -> None:
        self._closed = True
        try:
            self._writer.close()
        except Exception:  # noqa: BLE001
            pass
