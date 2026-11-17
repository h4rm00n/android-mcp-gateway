"""duration / end_time 语义回归：非循环事件必须持久化真实时长。

用法: uv run python scripts/test_duration.py [设备名 ...]
默认测全部在线设备。
"""

import asyncio
import sys

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

MCP_URL = "http://127.0.0.1:18437/mcp"
TOKEN = "mcp-dev-token-0000000000000000"


async def one(session: ClientSession, device: str) -> None:
    async def call(name: str, args: dict) -> dict:
        r = await session.call_tool(name, {"device": device, **args})
        if r.is_error:
            raise AssertionError(f"{name}: {r.content[0].text}")
        return r.structured_content

    def span(ev: dict) -> float:
        from datetime import datetime
        s = datetime.fromisoformat(ev["start_time"].replace("Z", "+00:00"))
        e = datetime.fromisoformat(ev["end_time"].replace("Z", "+00:00"))
        return (e - s).total_seconds() / 60

    async def read_back(event_id: int) -> dict:
        evs = await call(
            "calendar_list_events",
            {"start_time": "2026-12-01T00:00:00+08:00", "end_time": "2026-12-08T00:00:00+08:00"},
        )
        return next(e for e in evs["events"] if e["event_id"] == event_id)

    created = []
    try:
        # 1) duration 建 15 分钟 → 回读必须 15 分钟
        ev = await call(
            "calendar_create_event",
            {"title": "dur-15m", "start_time": "2026-12-02T10:00:00+08:00", "duration": "PT15M"},
        )
        created.append(ev["event_id"])
        got = await read_back(ev["event_id"])
        assert span(got) == 15, f"duration PT15M 未生效: {got['start_time']} → {got['end_time']} ({span(got)}min)"
        print(f"  ✓ duration=PT15M → 回读 {span(got):.0f} 分钟")

        # 2) duration 建 2 小时
        ev = await call(
            "calendar_create_event",
            {"title": "dur-2h", "start_time": "2026-12-02T12:00:00+08:00", "duration": "PT2H"},
        )
        created.append(ev["event_id"])
        got = await read_back(ev["event_id"])
        assert span(got) == 120, f"PT2H 未生效: {span(got)}min"
        print(f"  ✓ duration=PT2H → 回读 {span(got):.0f} 分钟")

        # 3) end_time 建 30 分钟（对照）
        ev = await call(
            "calendar_create_event",
            {"title": "end-30m", "start_time": "2026-12-03T17:00:00+08:00",
             "end_time": "2026-12-03T17:30:00+08:00"},
        )
        created.append(ev["event_id"])
        got = await read_back(ev["event_id"])
        assert span(got) == 30, f"end_time 未生效: {span(got)}min"
        print(f"  ✓ end_time → 回读 {span(got):.0f} 分钟")

        # 4) duration 建 → 用 end_time 更新（原报错场景）
        ev = await call(
            "calendar_create_event",
            {"title": "dur-then-end", "start_time": "2026-12-04T10:00:00+08:00", "duration": "PT20M"},
        )
        eid = ev["event_id"]
        created.append(eid)
        await call(
            "calendar_update_event",
            {"event_id": eid, "end_time": "2026-12-04T10:45:00+08:00"},
        )
        got = await read_back(eid)
        assert span(got) == 45, f"duration→end_time 更新失败: {span(got)}min"
        print(f"  ✓ duration 事件改用 end_time 更新 → {span(got):.0f} 分钟（不再报 DTEND/DURATION 冲突）")

        # 5) end_time 建 → 用 duration 更新
        ev = await call(
            "calendar_create_event",
            {"title": "end-then-dur", "start_time": "2026-12-05T10:00:00+08:00",
             "end_time": "2026-12-05T11:00:00+08:00"},
        )
        eid = ev["event_id"]
        created.append(eid)
        await call("calendar_update_event", {"event_id": eid, "duration": "PT45M"})
        got = await read_back(eid)
        assert span(got) == 45, f"end_time→duration 更新失败: {span(got)}min"
        print(f"  ✓ end_time 事件改用 duration 更新 → {span(got):.0f} 分钟")

        # 6) 只改标题：时长不变
        await call("calendar_update_event", {"event_id": eid, "title": "end-then-dur-changed"})
        got = await read_back(eid)
        assert span(got) == 45 and got["title"] == "end-then-dur-changed"
        print(f"  ✓ 仅改标题 → 时长保持 {span(got):.0f} 分钟")

        # 7) 只平移开始时间：时长不变
        await call("calendar_update_event", {"event_id": eid, "start_time": "2026-12-06T10:00:00+08:00"})
        got = await read_back(eid)
        assert span(got) == 45, f"平移后时长变化: {span(got)}min"
        print(f"  ✓ 平移开始时间 → 时长保持 {span(got):.0f} 分钟")

    finally:
        for eid in created:
            try:
                await call("calendar_delete_event", {"event_id": eid})
            except Exception as e:
                print(f"    清理 {eid} 失败: {e}")


async def main() -> None:
    devices = sys.argv[1:] or ["WayDroid-x86_64-Device", "M2007J1SC"]
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"})
    failed = False
    async with streamable_http_client(MCP_URL, http_client=http) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            for dev in devices:
                print(f"\n[{dev}]")
                try:
                    await one(session, dev)
                except AssertionError as e:
                    print(f"  ✗ {e}")
                    failed = True
    print("\n全部通过 ✓" if not failed else "\n有失败 ✗")
    sys.exit(1 if failed else 0)


asyncio.run(main())
