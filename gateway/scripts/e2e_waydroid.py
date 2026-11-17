"""E2E 联调脚本：宿主机 MCP 客户端 → 网关 → 设备 App → 真实设备能力。

用法:
    uv run python scripts/e2e_waydroid.py [设备名]

设备名默认 WayDroid-x86_64-Device（App 默认名 = 机型名，空白转 -）。
网关地址/token 可用环境变量覆盖（默认指向本地开发网关）:
    AMG_MCP_URL=http://127.0.0.1:18437/mcp AMG_MCP_TOKEN=xxx uv run python scripts/e2e_waydroid.py M2007J1SC

注意：脚本会**真在设备上建/改/删事件、发/划通知**，并在结束时删除自己建的事件。
"""

import asyncio
import json
import os
import sys

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

MCP_URL = os.environ.get("AMG_MCP_URL", "http://127.0.0.1:18437/mcp")
TOKEN = os.environ.get("AMG_MCP_TOKEN", "mcp-dev-token-0000000000000000")
DEVICE = sys.argv[1] if len(sys.argv) > 1 else "WayDroid-x86_64-Device"


async def call(session: ClientSession, name: str, args: dict, note: str) -> dict:
    args = {"device": DEVICE, **args}  # 每个工具强制指定目标设备
    result = await session.call_tool(name, args)
    if result.is_error:
        print(f"  ✗ {name} ({note}): {result.content[0].text}")
        sys.exit(1)
    body = result.structured_content
    print(f"  ✓ {name} ({note}): {json.dumps(body, ensure_ascii=False)[:200]}")
    return body


async def main() -> None:
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"})
    async with streamable_http_client(MCP_URL, http_client=http) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()

            tools = await session.list_tools()
            print(f"1. tools/list: {len(tools.tools)} 个工具 {[t.name for t in tools.tools]}")
            assert len(tools.tools) == 10  # 9 个设备工具 + list_devices

            print("2. device_status")
            status = await call(session, "device_status", {}, "真机状态")

            print("3. calendar_create_event（验收场景：周六聚会，提前1天+30分钟，method=ALARM）")
            event = await call(
                session,
                "calendar_create_event",
                {
                    "title": "餐厅聚会",
                    "location": "某餐厅",
                    "start_time": "2026-11-21T19:00:00+08:00",
                    "end_time": "2026-11-21T21:00:00+08:00",
                    "reminders": [1440, {"minutes": 30, "method": 4}],
                    "description": "E2E 联调验收事件",
                },
                "新建",
            )
            event_id = event["event_id"]

            print("4. calendar_list_events（Instances 展开校验）")
            events = await call(
                session,
                "calendar_list_events",
                {"start_time": "2026-11-21T00:00:00+08:00", "end_time": "2026-11-22T00:00:00+08:00"},
                "查回",
            )
            found = next(e for e in events["events"] if e["event_id"] == event_id)
            assert found["title"] == "餐厅聚会", found
            reminders = found["reminders"]
            methods = {r["method"] for r in reminders}
            assert 4 in methods, f"method=ALARM 丢失: {reminders}"
            print(f"    ↳ 提醒已入库: {reminders}（含 ALARM=4 透传）")

            print("5. calendar_update_event（改标题）")
            await call(
                session,
                "calendar_update_event",
                {"event_id": event_id, "title": "餐厅聚会（已改）"},
                "更新",
            )

            print("6. notification_post")
            await call(session, "notification_post", {"title": "测试通知", "body": "来自 AI 的通知"}, "发通知")
            await asyncio.sleep(1)

            print("7. notification_read → dismiss → read")
            read = await call(session, "notification_read", {"package_name": "dev.amg.device"}, "读")
            # MIUI 会给通知挂 ranker_group 分组项，按标题精确锁定我们的那条
            keys = [n["key"] for n in read["notifications"] if n["title"] == "测试通知"]
            assert keys, f"没读到测试通知，实际: {[(n['title'], n['text']) for n in read['notifications']]}"
            await call(session, "notification_dismiss", {"notification_key": keys[0]}, "划掉")
            read2 = await call(session, "notification_read", {"package_name": "dev.amg.device"}, "复查")
            assert all(n["key"] != keys[0] for n in read2["notifications"]), "划掉后仍在"

            print("8. calendar_delete_event（清理）")
            await call(session, "calendar_delete_event", {"event_id": event_id}, "删除")

            print("\n全部通过 ✓  MCP → 网关 → WS → Waydroid → 真实日历/通知 闭环成立")


asyncio.run(main())
