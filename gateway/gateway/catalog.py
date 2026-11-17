"""静态工具目录 —— 给 AI 看的工具契约（名称/描述/inputSchema/timeout）。

这是 `tools/list` 的唯一来源，与设备在线状态解耦（见 AGENTS.md「工具目录静态化」）。
设备 App 内持同一份 schema 做入参校验（协议 §6：args 校验只在设备端）。

修改任何工具的 schema/语义：本文件 bump TOOLSET_VERSION，设备 App 同步 bump。
"""

from __future__ import annotations

from typing import Any

# 工具契约版本。与设备 App 中的常量必须一致；hello 不一致 → TOOLSET_MISMATCH。
TOOLSET_VERSION = 1

PROTO_VERSION = 1

REMINDER_METHODS = {
    0: "DEFAULT（由日历 App 决定，一般等同 ALERT）",
    1: "ALERT（通用，受勿扰等规则影响）",
    2: "EMAIL（仅 Google 账户日历）",
    3: "SMS（已废弃）",
    4: "ALARM（真闹钟；小米日历下无视勿扰）",
}

_ISO = "ISO 8601 本地时间带时区偏移，如 2026-11-21T19:00:00+08:00（或 Z 结尾 UTC）"


def _reminder_method_enum() -> dict[str, Any]:
    doc = "；".join(f"{k}={v}" for k, v in REMINDER_METHODS.items())
    return {
        "type": "integer",
        "enum": list(REMINDER_METHODS),
        "description": f"提醒方式，原样透传给日历 App 不做校验降级。{doc}",
    }


def _reminders_schema() -> dict[str, Any]:
    return {
        "description": (
            "提前提醒。整数即提前分钟数（method 默认 ALERT）；"
            "需要指定提醒方式时用对象 {minutes, method}，method=4 为真闹钟（目标设备小米日历无视勿扰）。"
            "多条提醒即多个元素。真正发提醒的是管理该日历的日历 App。"
        ),
        "type": "array",
        "items": {
            "oneOf": [
                {"type": "integer", "minimum": 0},
                {
                    "type": "object",
                    "properties": {
                        "minutes": {"type": "integer", "minimum": 0},
                        "method": _reminder_method_enum(),
                    },
                    "required": ["minutes"],
                    "additionalProperties": False,
                },
            ]
        },
    }


def _availability_schema() -> dict[str, Any]:
    return {
        "type": "string",
        "enum": ["BUSY", "FREE", "TENTATIVE"],
        "description": "忙闲状态，默认 BUSY",
    }


_CALENDAR_ID = {
    "type": "integer",
    "description": "目标日历 id（来自 calendar_list_calendars 的 _id）；缺省用主日历",
}

# 事件入参的公共字段（create 全量，update 同集全可选）
_EVENT_FIELDS: dict[str, Any] = {
    "title": {"type": "string", "description": "事件标题", "minLength": 1},
    "description": {"type": "string", "description": "事件描述"},
    "location": {"type": "string", "description": "地点"},
    "start_time": {"type": "string", "description": f"开始时间，{_ISO}"},
    "end_time": {
        "type": "string",
        "description": f"结束时间，{_ISO}。与 duration 二选一",
    },
    "duration": {
        "type": "string",
        "description": (
            "RFC 5545 时长，如 PT2H / P1D。与 end_time 二选一；"
            "循环事件（提供 recurrence 时）必须用 duration，不能用 end_time"
        ),
    },
    "all_day": {
        "type": "boolean",
        "description": "全天事件（默认 false）。全天事件的时区/UTC 零点转换由设备端处理，AI 不感知",
    },
    "timezone": {
        "type": "string",
        "description": "IANA 时区名（如 Asia/Shanghai），缺省设备时区",
    },
    "recurrence": {
        "type": "string",
        "description": 'RFC 5545 RRULE 规则体（不含 "RRULE:" 前缀），如 "FREQ=WEEKLY;BYDAY=MO,WE"',
    },
    "reminders": _reminders_schema(),
    "calendar_id": _CALENDAR_ID,
    "availability": _availability_schema(),
}

_LIST_EVENTS_RESULT_NOTE = (
    "返回 {events: [...]}：每项含 event_id、title、start_time、end_time、"
    "all_day、location、description、calendar_id、availability、reminders；"
    "循环事件的实例带 original_id 与 original_instance_time"
)

_TOOLSETS: list[dict[str, Any]] = [
    # ---- 日历 ----
    {
        "name": "calendar_list_calendars",
        "description": (
            "列出设备上所有日历账户：_id、名称、是否主日历、访问级别（>=500 可写）、"
            "颜色、账户。创建事件前不确定用哪个日历时先调这个。"
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "timeout_ms": 10_000,
    },
    {
        "name": "calendar_list_events",
        "description": (
            "按时间范围列出日历事件，循环事件已按实例展开（内部查 Instances 表）。"
            f"{_LIST_EVENTS_RESULT_NOTE}。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "start_time": {"type": "string", "description": f"范围起点，{_ISO}"},
                "end_time": {"type": "string", "description": f"范围终点，{_ISO}"},
                "calendar_id": _CALENDAR_ID,
            },
            "required": ["start_time", "end_time"],
            "additionalProperties": False,
        },
        "timeout_ms": 15_000,
    },
    {
        "name": "calendar_create_event",
        "description": (
            "创建日历事件，可附带多条提醒（reminders.method=4 在小米日历上是无视勿扰的真闹钟，"
            "可当闹钟用）。返回 {event_id}。"
            "end_time 与 duration 必须提供其一。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": _EVENT_FIELDS,
            "required": ["title", "start_time"],
            # 提供 duration 时不要求 end_time；否则要求
            "allOf": [
                {
                    "if": {"not": {"required": ["duration"]}},
                    "then": {"required": ["end_time"]},
                },
                # 循环事件必须 duration，不能 end_time（Provider 对 rrule+dtend 的展开不可靠）
                {
                    "if": {"required": ["recurrence"]},
                    "then": {"not": {"required": ["end_time"]}},
                },
                {"not": {"allOf": [{"required": ["end_time"]}, {"required": ["duration"]}]}},
            ],
            "additionalProperties": False,
        },
        "timeout_ms": 15_000,
    },
    {
        "name": "calendar_update_event",
        "description": (
            "按 event_id 更新事件（calendar_list_events 返回的 event_id）。"
            "未提供的字段保持不变；提供 reminders 则整体替换全部提醒。"
            "MVP 阶段对循环事件的更新作用于整串（单实例例外记录是二期）。返回 {event_id}。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"event_id": {"type": "integer", "description": "目标事件 id"}, **_EVENT_FIELDS},
            "required": ["event_id"],
            "additionalProperties": False,
        },
        "timeout_ms": 15_000,
    },
    {
        "name": "calendar_delete_event",
        "description": (
            "按 event_id 删除事件。传 occurrence_start 时只删除循环事件的该次实例"
            "（内部写 EXDATE 排除，循环规则与其它实例不受影响）；不传则删除整串。"
            "返回 {deleted: true, recurring: bool}。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "integer", "description": "目标事件 id"},
                "occurrence_start": {
                    "type": "string",
                    "description": (
                        "要删除的单次开始时间（ISO 8601，取自 calendar_list_events 的 start_time）。"
                        "仅对循环事件有效；省略表示删除整个事件（循环事件=整串）"
                    ),
                },
            },
            "required": ["event_id"],
            "additionalProperties": False,
        },
        "timeout_ms": 10_000,
    },
    # ---- 通知 ----
    {
        "name": "notification_read",
        "description": (
            "读取当前通知栏快照（最多 limit 条，按时间倒序）。每项含 key（唯一标识，dismiss 用）、"
            "package（来源包名）、app（App 显示名）、title、text（聊天类已展开到具体消息）、"
            "可选 sub_text / conversation_title / category、post_time、ongoing（是否常驻）。"
            "**通知栏里常见空壳通知**（分组占位、媒体控制等无正文的条目）——"
            "要精确找内容（如验证码）时传 non_empty_only=true 过滤掉它们。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "最多返回条数，默认 20"},
                "package_name": {"type": "string", "description": "只看指定 App（包名），如 com.tencent.mm"},
                "include_ongoing": {
                    "type": "boolean",
                    "description": "是否包含常驻通知（音乐播放器、前台服务等），默认 true",
                },
                "non_empty_only": {
                    "type": "boolean",
                    "description": "只返回有标题或正文的通知（过滤分组占位等空壳），默认 false",
                },
            },
            "additionalProperties": False,
        },
        "timeout_ms": 10_000,
    },
    {
        "name": "notification_dismiss",
        "description": "划掉（移除）一条通知。key 来自 notification_read。系统/常驻（ongoing）通知可能拒绝。返回 {dismissed: true}。",
        "inputSchema": {
            "type": "object",
            "properties": {"notification_key": {"type": "string", "description": "notification_read 返回的 key"}},
            "required": ["notification_key"],
            "additionalProperties": False,
        },
        "timeout_ms": 10_000,
    },
    {
        "name": "notification_post",
        "description": "以 Agent 自身名义在手机上发一条通知（普通通知，非闹钟；定时场景用日历提醒 method=ALARM）。返回 {posted: true}。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "通知标题", "minLength": 1},
                "body": {"type": "string", "description": "通知正文"},
            },
            "required": ["title"],
            "additionalProperties": False,
        },
        "timeout_ms": 10_000,
    },
    # ---- 设备 ----
    {
        "name": "device_status",
        "description": (
            "设备状态快照：model、android 版本、app_version、battery_level、charging、"
            "以及设备与网关的连接状态。怀疑设备离线时先调这个。"
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "timeout_ms": 5_000,
    },
]

TOOLS: tuple[dict[str, Any], ...] = tuple(_TOOLSETS)
TOOL_NAMES: frozenset[str] = frozenset(t["name"] for t in TOOLS)
_TOOL_TIMEOUTS: dict[str, int] = {t["name"]: t["timeout_ms"] for t in TOOLS}

# ---- 网关本地工具（在网关内回答，不经过设备；不影响 TOOLSET_VERSION）----
def _pairing_tool_enabled() -> bool:
    """配对工具只在启用裸 TCP 传输时暴露（WS 模式不需要配对码）。"""
    return True


GATEWAY_LOCAL_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "list_devices",
        "description": (
            "列出网关已知的所有设备及其在线状态（含离线设备的最后在线时间与机型）。"
            "**调用任何其它工具前先调这个**，用它返回的 device 值填其它工具的 device 参数。"
            "不需要 device 参数。"
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "timeout_ms": 5_000,
    },
    {
        "name": "generate_pairing_code",
        "description": (
            "生成一个设备配对码（8 字符，如 K7M2-P9XR），用于让新设备通过裸 TCP+Noise 通道配对。"
            "拿到码后把它念给用户：用户会填到手机 App 里完成配对。"
            "码 5 分钟内有效、只能使用一次。不需要 device 参数。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "device_name": {
                    "type": "string",
                    "description": "设备将要使用的名字（如 mi14），仅用于标注，不是强绑定",
                }
            },
            "additionalProperties": False,
        },
        "timeout_ms": 5_000,
    },
    {
        "name": "list_paired_devices",
        "description": (
            "列出已配对（持有设备公钥）的设备：名字、配对于何时、是否改名过、当前是否在线。"
            "用于核对配对记录、清理陈旧条目。不需要 device 参数。"
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "timeout_ms": 5_000,
    },
    {
        "name": "unpair_device",
        "description": (
            "吊销一台已配对设备（删除其公钥）。吊销后该设备无法再建立 Noise 会话，"
            "需要重新配对。用于设备丢失/换机/密钥泄露。不需要 device 参数。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "要吊销的设备名（来自 list_paired_devices）",
                }
            },
            "required": ["target"],
            "additionalProperties": False,
        },
        "timeout_ms": 5_000,
    },
)


def timeout_for(name: str) -> int:
    for t in GATEWAY_LOCAL_TOOLS:
        if t["name"] == name:
            return t["timeout_ms"]
    return _TOOL_TIMEOUTS.get(name, 30_000)
