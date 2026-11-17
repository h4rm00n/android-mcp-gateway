package dev.amg.device

import org.json.JSONObject
import java.time.OffsetDateTime
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter

/** AI 入参 ISO 8601 时间 ↔ 日历 Provider 的 epoch 毫秒/时区转换（AGENTS.md「字段要点」）。 */
object TimeParse {

    fun parse(value: String): OffsetDateTime = OffsetDateTime.parse(
        value.trim(),
        DateTimeFormatter.ISO_OFFSET_DATE_TIME,
    )

    /** 普通事件：epoch ms + 事件时区（入参 timezone 缺省设备时区）。 */
    fun toEpochMs(odt: OffsetDateTime, tzOverride: String?): Pair<Long, String> {
        val zoneId = tzOverride?.takeIf { it.isNotBlank() } ?: java.time.ZoneId.systemDefault().id
        val zoned = odt.atZoneSameInstant(java.time.ZoneId.of(zoneId))
        return zoned.toInstant().toEpochMilli() to zoneId
    }

    /**
     * 全天事件（AGENTS.md 钉死的转换，AI 不感知）：
     * dtstart 取该日期的 UTC 零点 + duration=P1D + allDay=1 + eventTimezone=UTC，
     * 否则跨时区偏移一天。
     */
    fun toAllDayUtcStart(value: String): Long =
        parse(value).toLocalDate().atStartOfDay(ZoneOffset.UTC).toInstant().toEpochMilli()

    /** epoch ms → ISO 8601（事件自身时区）。 */
    fun format(epochMs: Long, tzId: String?): String {
        val zone = runCatching { java.time.ZoneId.of(tzId) }.getOrNull() ?: java.time.ZoneId.systemDefault()
        return java.time.Instant.ofEpochMilli(epochMs).atZone(zone)
            .format(DateTimeFormatter.ISO_OFFSET_DATE_TIME)
    }

    /** args 取可选字符串字段。 */
    fun optString(args: JSONObject, key: String): String? =
        if (args.has(key) && !args.isNull(key)) args.getString(key) else null
}
