"""目录自检：schema 结构合法、契约内部一致。"""

from gateway.catalog import TOOL_NAMES, TOOLS, timeout_for


def test_unique_names():
    names = [t["name"] for t in TOOLS]
    assert len(names) == len(set(names))


def test_expected_mvp_tools():
    assert TOOL_NAMES == {
        "calendar_list_calendars",
        "calendar_list_events",
        "calendar_create_event",
        "calendar_update_event",
        "calendar_delete_event",
        "notification_read",
        "notification_dismiss",
        "notification_post",
        "device_status",
    }


def test_schema_shape_and_timeouts():
    for t in TOOLS:
        assert t["inputSchema"]["type"] == "object"
        assert isinstance(t["description"], str) and t["description"]
        assert 5_000 <= t["timeout_ms"] <= 120_000
        assert timeout_for(t["name"]) == t["timeout_ms"]


def test_create_event_constraints():
    create = next(t for t in TOOLS if t["name"] == "calendar_create_event")
    schema = create["inputSchema"]
    assert "title" in schema["required"] and "start_time" in schema["required"]
    # 循环事件必须 duration 不能 end_time；end_time/duration 互斥
    conditions = [c for c in schema["allOf"] if "if" in c]
    assert len(conditions) == 2
    mutual = [c for c in schema["allOf"] if "not" in c and "if" not in c]
    assert len(mutual) == 1
