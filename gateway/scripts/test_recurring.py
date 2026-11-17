"""循环事件回归：展开、单实例删除（EXDATE）、整串删除、更新不破坏循环规则。

用法: uv run python scripts/test_recurring.py [设备名 ...]
默认测全部在线设备。
"""

import asyncio
import sys

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

MCP_URL = "http://127.0.0.1:18437/mcp"
TOKEN = "mcp-dev-token-0000000000000000"
RANGE = {"start_time": "2026-12-01T00:00:00+08:00", "end_time": "2027-02-01T00:00:00+08:00"}
DEFAULT_DEVICES = ["WayDroid-x86_64-Device", "M2007J1SC"]


async def one(session: ClientSession, device: str) -> None:
    async def call(name: str, args: dict) -> dict:
        r = await session.call_tool(name, {"device": device, **args})
        if r.is_error:
            raise AssertionError(f"{name}: {r.content[0].text}")
        return r.structured_content

    async def occurrences(title_prefix: str) -> list[dict]:
        evs = await call("calendar_list_events", RANGE)
        return [e for e in evs["events"] if e["title"].startswith(title_prefix)]

    created: list[int] = []
    try:
        # 1) 一周两次课：MO+WE，COUNT=6 → 6 个实例
        ev = await call("calendar_create_event", {
            "title": "回归-循环A", "start_time": "2026-12-07T08:00:00+08:00",
            "duration": "PT1H40M", "recurrence": "FREQ=WEEKLY;BYDAY=MO,WE;COUNT=6",
        })
        eid = ev["event_id"]
        created.append(eid)
        inst = await occurrences("回归-循环A")
        assert len(inst) == 6, f"应展开 6 个实例，实际 {len(inst)}"
        for e in inst:
            assert (e["end_time"], e["start_time"]) != (e["start_time"], e["start_time"]), "时长归零"
        print(f"  ✓ 建循环 MO+WE,COUNT=6 → 展开 {len(inst)} 个实例，每个 100 分钟")

        # 2) 隔周课 INTERVAL=2
        ev = await call("calendar_create_event", {
            "title": "回归-循环B", "start_time": "2026-12-01T08:00:00+08:00",
            "duration": "PT1H40M", "recurrence": "FREQ=WEEKLY;INTERVAL=2;BYDAY=TU;COUNT=4",
        })
        eid_b = ev["event_id"]
        created.append(eid_b)
        inst_b = await occurrences("回归-循环B")
        assert len(inst_b) == 4, f"隔周课应 4 个实例，实际 {len(inst_b)}"
        days = sorted(e["start_time"][:10] for e in inst_b)
        print(f"  ✓ 隔周课 INTERVAL=2,COUNT=4 → {len(inst_b)} 个实例 {days}")

        # 3) 单实例删除：删第 3 个实例，其余保持
        target = inst[2]["start_time"]
        r = await call("calendar_delete_event", {"event_id": eid, "occurrence_start": target})
        assert r["recurring"] is True
        after = await occurrences("回归-循环A")
        assert len(after) == 5, f"删一个实例后应剩 5，实际 {len(after)}"
        remaining_starts = {e["start_time"] for e in after}
        assert target not in remaining_starts, f"目标实例仍在: {target}"
        for e in after:
            assert e["end_time"] != e["start_time"], "删除后时长被破坏"
        print(f"  ✓ 单实例删除 → 剩 {len(after)} 个实例，目标已排除，其余时长完好")

        # 4) 再删一个，验证 EXDATE 累积（多值列表）
        target2 = after[0]["start_time"]
        await call("calendar_delete_event", {"event_id": eid, "occurrence_start": target2})
        after2 = await occurrences("回归-循环A")
        assert len(after2) == 4, f"删两个后应剩 4，实际 {len(after2)}"
        assert target not in {e["start_time"] for e in after2}
        assert target2 not in {e["start_time"] for e in after2}
        print(f"  ✓ 再删一个 → 剩 {len(after2)} 个（EXDATE 累积生效）")

        # 5) 更新循环事件时长不破坏循环规则
        await call("calendar_update_event", {"event_id": eid_b, "duration": "PT2H"})
        inst_b2 = await occurrences("回归-循环B")
        assert len(inst_b2) == 4, f"改时长后循环规则被破坏：实例数 {len(inst_b2)}"
        print(f"  ✓ 更新循环事件时长 → 实例数保持 {len(inst_b2)}")

        # 6) 更新循环事件标题不破坏循环规则
        await call("calendar_update_event", {"event_id": eid_b, "title": "回归-循环B-改名"})
        inst_b3 = await occurrences("回归-循环B")
        assert len(inst_b3) == 4, f"改标题后循环规则被破坏：{len(inst_b3)}"
        print(f"  ✓ 更新标题 → 实例数保持 {len(inst_b3)}")

        # 7) 整串删除
        r = await call("calendar_delete_event", {"event_id": eid})
        assert r["recurring"] is False
        assert not await occurrences("回归-循环A"), "整串删除后仍有残留"
        created.remove(eid)
        print("  ✓ 整串删除 → 0 实例")

        # 8) 非循环事件传 occurrence_start 应报错
        ev = await call("calendar_create_event", {
            "title": "回归-单次", "start_time": "2026-12-20T10:00:00+08:00", "duration": "PT30M",
        })
        eid_c = ev["event_id"]
        created.append(eid_c)
        r = await session.call_tool("calendar_delete_event", {
            "device": device, "event_id": eid_c, "occurrence_start": "2026-12-20T10:00:00+08:00",
        })
        assert r.is_error and "occurrence_start" in r.content[0].text
        print("  ✓ 非循环事件传 occurrence_start → 明确报错（不误删）")

    finally:
        for e in created:
            try:
                await call("calendar_delete_event", {"event_id": e})
            except Exception as exc:
                print(f"    清理 {e} 失败: {exc}")


async def main() -> None:
    devices = sys.argv[1:] or DEFAULT_DEVICES
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
