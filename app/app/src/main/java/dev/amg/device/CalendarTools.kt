package dev.amg.device

import android.content.ContentValues
import android.content.Context
import android.provider.CalendarContract.Calendars
import android.provider.CalendarContract.Events
import android.provider.CalendarContract.Instances
import android.provider.CalendarContract.Reminders
import org.json.JSONArray
import org.json.JSONObject

/** 日历工具×5。查 Instances 展开循环事件；循环事件必须 duration（AGENTS.md「字段要点」）。 */
object CalendarTools {

    // 访问级别 >=500（CONTRIBUTOR）可写
    private const val ACCESS_CONTRIBUTOR = 500

    fun register() {
        ToolRegistry.register("calendar_list_calendars", ::listCalendars)
        ToolRegistry.register("calendar_list_events", ::listEvents)
        ToolRegistry.register("calendar_create_event", ::createEvent)
        ToolRegistry.register("calendar_update_event", ::updateEvent)
        ToolRegistry.register("calendar_delete_event", ::deleteEvent)
    }

    // ---- 校验权限的公共入口 ----

    private fun ensureCalendarPermission(ctx: Context, write: Boolean) {
        val perm = if (write) "android.permission.WRITE_CALENDAR" else "android.permission.READ_CALENDAR"
        if (ctx.checkSelfPermission(perm) != android.content.pm.PackageManager.PERMISSION_GRANTED) {
            throw ToolError("PERMISSION_DENIED", "$perm 未授予")
        }
    }

    // ---- calendar_list_calendars ----

    private fun listCalendars(ctx: Context, args: JSONObject): JSONObject {
        ensureCalendarPermission(ctx, write = false)
        val cr = ctx.contentResolver
        val projection = arrayOf(
            Calendars._ID, Calendars.CALENDAR_DISPLAY_NAME, Calendars.IS_PRIMARY,
            Calendars.CALENDAR_ACCESS_LEVEL, Calendars.CALENDAR_COLOR, Calendars.ACCOUNT_NAME,
        )
        val out = JSONArray()
        cr.query(Calendars.CONTENT_URI, projection, null, null, null)?.use { c ->
            while (c.moveToNext()) {
                out.put(
                    JSONObject()
                        .put("_id", c.getLong(0))
                        .put("calendarDisplayName", c.getString(1) ?: "")
                        .put("isPrimary", c.getInt(2) == 1)
                        .put("calendarAccessLevel", c.getInt(3))
                        .put("color", c.getInt(4))
                        .put("accountName", c.getString(5) ?: "")
                )
            }
        }
        return JSONObject().put("calendars", out).put("count", out.length())
    }

    /** 缺省日历：优先 isPrimary=1，其次访问级别最高的可写日历；一个都没有则自举本地日历。 */
    private fun resolveDefaultCalendarId(ctx: Context): Long {
        val cr = ctx.contentResolver
        val projection = arrayOf(Calendars._ID, Calendars.IS_PRIMARY, Calendars.CALENDAR_ACCESS_LEVEL)
        var primaryId: Long? = null
        var bestWritable: Pair<Int, Long>? = null
        cr.query(Calendars.CONTENT_URI, projection, null, null, null)?.use { c ->
            while (c.moveToNext()) {
                val id = c.getLong(0)
                val primary = c.getInt(1) == 1
                val level = c.getInt(2)
                if (primary && primaryId == null) primaryId = id
                if (level >= ACCESS_CONTRIBUTOR && (bestWritable == null || level > bestWritable!!.first)) {
                    bestWritable = level to id
                }
            }
        }
        return primaryId ?: bestWritable?.second ?: bootstrapLocalCalendar(ctx)
    }

    /**
     * 全新系统（如 Waydroid 容器）没有任何日历账户时，创建一个 LOCAL 日历兜底。
     * 普通应用写 Calendars 需 caller_is_syncadapter=true + LOCAL 账户（AOSP 惯例）。
     */
    private fun bootstrapLocalCalendar(ctx: Context): Long {
        val accountName = "amg.local"
        val cv = ContentValues().apply {
            put(Calendars.NAME, "amg-local")
            put(Calendars.CALENDAR_DISPLAY_NAME, "AMG Local")
            put(Calendars.CALENDAR_COLOR, -0x3D3D3E) // Material Indigo
            put(Calendars.CALENDAR_ACCESS_LEVEL, Calendars.CAL_ACCESS_OWNER)
            put(Calendars.ACCOUNT_NAME, accountName)
            put(Calendars.ACCOUNT_TYPE, android.provider.CalendarContract.ACCOUNT_TYPE_LOCAL)
            put(Calendars.OWNER_ACCOUNT, accountName)
            put(Calendars.SYNC_EVENTS, 1)
            put(Calendars.VISIBLE, 1)
            put(Calendars.IS_PRIMARY, 1)
        }
        val uri = Calendars.CONTENT_URI.buildUpon()
            .appendQueryParameter(android.provider.CalendarContract.CALLER_IS_SYNCADAPTER, "true")
            .appendQueryParameter(Calendars.ACCOUNT_NAME, accountName)
            .appendQueryParameter(Calendars.ACCOUNT_TYPE, android.provider.CalendarContract.ACCOUNT_TYPE_LOCAL)
            .build()
        val inserted = ctx.contentResolver.insert(uri, cv)
            ?: throw ToolError("DEVICE_INTERNAL", "创建本地日历失败")
        return inserted.lastPathSegment?.toLongOrNull()
            ?: throw ToolError("DEVICE_INTERNAL", "创建本地日历返回异常")
    }

    // ---- calendar_list_events（Instances 表，循环事件已展开）----

    private fun listEvents(ctx: Context, args: JSONObject): JSONObject {
        ensureCalendarPermission(ctx, write = false)
        val startMs = epochOf(TimeParse.parse(ToolRegistry.requireString(args, "start_time")))
        val endMs = epochOf(TimeParse.parse(ToolRegistry.requireString(args, "end_time")))
        if (endMs <= startMs) throw ToolError("ARGS_INVALID", "end_time 必须晚于 start_time")
        val calendarId = ToolRegistry.optLong(args, "calendar_id")

        // Instances.CONTENT_URI 已是 .../instances/when，追加范围即可（勿重复 append "when"）
        val uri = Instances.CONTENT_URI.buildUpon()
            .appendPath(startMs.toString())
            .appendPath(endMs.toString())
            .build()
        val projection = arrayOf(
            Instances.EVENT_ID, Instances.TITLE, Instances.DESCRIPTION, Instances.EVENT_LOCATION,
            Instances.BEGIN, Instances.END, Instances.ALL_DAY, Instances.CALENDAR_ID,
            Instances.AVAILABILITY, Instances.ORIGINAL_ID, Instances.ORIGINAL_INSTANCE_TIME,
            Instances.EVENT_TIMEZONE,
        )
        val selection = if (calendarId != null) "${Instances.CALENDAR_ID}=?" else null
        val selectionArgs = if (calendarId != null) arrayOf(calendarId.toString()) else null

        val cr = ctx.contentResolver
        val rows = JSONArray()
        val eventIds = mutableListOf<Long>()
        cr.query(uri, projection, selection, selectionArgs, "${Instances.BEGIN} ASC")?.use { c ->
            while (c.moveToNext()) {
                val tz = c.getString(11)
                val row = JSONObject()
                    .put("event_id", c.getLong(0))
                    .put("title", c.getString(1) ?: "")
                    .put("description", c.getString(2) ?: "")
                    .put("location", c.getString(3) ?: "")
                    .put("start_time", TimeParse.format(c.getLong(4), tz))
                    .put("end_time", TimeParse.format(c.getLong(5), tz))
                    .put("all_day", c.getInt(6) == 1)
                    .put("calendar_id", c.getLong(7))
                    .put("availability", availabilityName(c.getInt(8)))
                if (!c.isNull(9)) row.put("original_id", c.getLong(9))
                if (!c.isNull(10)) row.put("original_instance_time", TimeParse.format(c.getLong(10), tz))
                rows.put(row)
                eventIds.add(c.getLong(0))
            }
        }

        // 批量取提醒（一条查询）
        val remindersByEvent = mutableMapOf<Long, JSONArray>()
        if (eventIds.isNotEmpty()) {
            val inClause = eventIds.joinToString(",")
            cr.query(
                Reminders.CONTENT_URI,
                arrayOf(Reminders.EVENT_ID, Reminders.MINUTES, Reminders.METHOD),
                "${Reminders.EVENT_ID} IN ($inClause)", null, null
            )?.use { c ->
                while (c.moveToNext()) {
                    val eventId = c.getLong(0)
                    val arr = remindersByEvent.getOrPut(eventId) { JSONArray() }
                    arr.put(
                        JSONObject()
                            .put("minutes", c.getInt(1))
                            .put("method", c.getInt(2))
                    )
                }
            }
        }
        for (i in 0 until rows.length()) {
            val row = rows.getJSONObject(i)
            row.put("reminders", remindersByEvent[row.getLong("event_id")] ?: JSONArray())
        }

        return JSONObject().put("events", rows).put("count", rows.length())
    }

    // ---- calendar_create_event ----

    private fun createEvent(ctx: Context, args: JSONObject): JSONObject {
        ensureCalendarPermission(ctx, write = true)
        val cv = buildCreateValues(args)

        val calendarId = ToolRegistry.optLong(args, "calendar_id")
            ?: resolveDefaultCalendarId(ctx)
        cv.put(Events.CALENDAR_ID, calendarId)

        val cr = ctx.contentResolver
        val uri = cr.insert(Events.CONTENT_URI, cv)
            ?: throw ToolError("DEVICE_INTERNAL", "插入事件失败")
        val eventId = uri.lastPathSegment?.toLongOrNull()
            ?: throw ToolError("DEVICE_INTERNAL", "插入事件返回异常")

        insertReminders(cr, eventId, args)
        return JSONObject().put("event_id", eventId)
    }

    /**
     * 新建事件的 ContentValues。
     *
     * 时长表示（关键）：**DURATION 只对循环事件有意义**。非循环事件只写 DURATION 不写 DTEND 时，
     * Provider 会把 DTEND 置为等于 DTSTART（时长 0），读取端也不做换算——必须在此换算成 DTEND。
     */
    private fun buildCreateValues(args: JSONObject): ContentValues {
        val cv = ContentValues()
        val allDay = ToolRegistry.optBool(args, "all_day", default = false)
        val timezone = ToolRegistry.optString(args, "timezone")
        val hasEnd = args.has("end_time") && !args.isNull("end_time")
        val hasDuration = args.has("duration") && !args.isNull("duration")
        val recurrence = ToolRegistry.optString(args, "recurrence")

        cv.put(Events.TITLE, ToolRegistry.requireString(args, "title"))
        if (!hasEnd && !hasDuration && !allDay) {
            throw ToolError("ARGS_INVALID", "end_time 与 duration 必须提供其一（全天事件可省略，默认 1 天）")
        }
        if (hasEnd && hasDuration) throw ToolError("ARGS_INVALID", "end_time 与 duration 只能提供其一")
        if (recurrence != null && hasEnd) {
            // Provider 对 rrule+dtend 展开不可靠，且循环事件不允许 DTEND
            throw ToolError("ARGS_INVALID", "循环事件必须用 duration，不能用 end_time")
        }

        ToolRegistry.optString(args, "description")?.let { cv.put(Events.DESCRIPTION, it) }
        ToolRegistry.optString(args, "location")?.let { cv.put(Events.EVENT_LOCATION, it) }

        val startRaw = ToolRegistry.requireString(args, "start_time")
        val startMs: Long
        if (allDay) {
            startMs = TimeParse.toAllDayUtcStart(startRaw)
            cv.put(Events.ALL_DAY, 1)
            cv.put(Events.EVENT_TIMEZONE, "UTC")
        } else {
            val (ms, tz) = TimeParse.toEpochMs(TimeParse.parse(startRaw), timezone)
            startMs = ms
            cv.put(Events.EVENT_TIMEZONE, tz)
        }
        cv.put(Events.DTSTART, startMs)

        // 全天事件未给时长时默认 1 天（AGENTS.md：P1D）
        val durationSpec = when {
            hasDuration -> ToolRegistry.requireString(args, "duration")
            allDay && !hasEnd -> "P1D"
            else -> null
        }

        when {
            durationSpec != null -> {
                if (recurrence != null) {
                    cv.put(Events.DURATION, durationSpec) // 循环事件：DURATION 是唯一合法表示
                } else {
                    cv.put(Events.DTEND, startMs + parseDuration(durationSpec)) // 非循环：换算成 DTEND
                }
            }
            else -> {
                val endRaw = ToolRegistry.requireString(args, "end_time")
                val endMs = if (allDay) TimeParse.toAllDayUtcStart(endRaw) else epochOf(TimeParse.parse(endRaw))
                if (endMs <= startMs) throw ToolError("ARGS_INVALID", "end_time 必须晚于 start_time")
                cv.put(Events.DTEND, endMs)
            }
        }

        recurrence?.let { cv.put(Events.RRULE, it) }
        applyAvailability(cv, args)
        return cv
    }

    // ---- calendar_update_event（MVP：作用于整串）----

    private fun updateEvent(ctx: Context, args: JSONObject): JSONObject {
        ensureCalendarPermission(ctx, write = true)
        val eventId = ToolRegistry.requireLong(args, "event_id")
        val existing = loadExistingEvent(ctx, eventId)
        val cv = buildUpdateValues(args, existing)

        val cr = ctx.contentResolver
        if (cv.size() > 0) {
            val updated = cr.update(Events.CONTENT_URI, cv, "${Events._ID}=?", arrayOf(eventId.toString()))
            if (updated == 0) throw ToolError("DEVICE_INTERNAL", "事件不存在: $eventId")
        }
        if (args.has("reminders")) {
            cr.delete(Reminders.CONTENT_URI, "${Reminders.EVENT_ID}=?", arrayOf(eventId.toString()))
            insertReminders(cr, eventId, args)
        }
        return JSONObject().put("event_id", eventId)
    }

    /** 已有事件的时长相关字段，用于更新时决定如何互斥改写 DTEND / DURATION。 */
    private data class ExistingEvent(
        val dtstart: Long?,
        val dtend: Long?,
        val duration: String?,
        val rrule: String?,
        val allDay: Boolean,
        val exdate: String? = null,
        val timezone: String? = null,
    )

    private fun loadExistingEvent(ctx: Context, eventId: Long): ExistingEvent {
        val cr = ctx.contentResolver
        val projection = arrayOf(
            Events.DTSTART, Events.DTEND, Events.DURATION, Events.RRULE, Events.ALL_DAY,
            Events.EXDATE, Events.EVENT_TIMEZONE,
        )
        cr.query(Events.CONTENT_URI, projection, "${Events._ID}=?", arrayOf(eventId.toString()), null)?.use { c ->
            if (c.moveToFirst()) {
                return ExistingEvent(
                    dtstart = if (c.isNull(0)) null else c.getLong(0),
                    dtend = if (c.isNull(1)) null else c.getLong(1),
                    duration = if (c.isNull(2)) null else c.getString(2),
                    rrule = if (c.isNull(3)) null else c.getString(3),
                    allDay = c.getInt(4) == 1,
                    exdate = if (c.isNull(5)) null else c.getString(5),
                    timezone = if (c.isNull(6)) null else c.getString(6),
                )
            }
        }
        throw ToolError("DEVICE_INTERNAL", "事件不存在: $eventId")
    }

    /**
     * 更新事件的 ContentValues。
     *
     * 两条 Provider 硬约束（实机验证，Android 13 / MIUI）：
     * 1. 同一事件禁止同时存在 DTEND 与 DURATION → 改写时必须把另一种 putNull。
     * 2. **Instances 表只在 DTSTART 参与 update 时才重算**。只改 DTEND/DURATION 时
     *    Instances 会保留旧值，`calendar_list_events` 因而读回旧时长。所以只要动了时间，
     *    就必须把 DTSTART 一并写入（哪怕值不变）以强制重建实例。
     */
    private fun buildUpdateValues(args: JSONObject, existing: ExistingEvent): ContentValues {
        val cv = ContentValues()
        val allDay = if (args.has("all_day")) {
            ToolRegistry.optBool(args, "all_day", default = existing.allDay).also {
                cv.put(Events.ALL_DAY, if (it) 1 else 0)
            }
        } else existing.allDay
        val newRecurrence = ToolRegistry.optString(args, "recurrence")
        val effectiveRecurrence = newRecurrence ?: existing.rrule

        val hasDuration = args.has("duration") && !args.isNull("duration")
        val hasEnd = args.has("end_time") && !args.isNull("end_time")
        if (hasDuration && hasEnd) throw ToolError("ARGS_INVALID", "end_time 与 duration 只能提供其一")

        // ---- 开始时间 ----
        var start = existing.dtstart
        if (args.has("start_time")) {
            val raw = ToolRegistry.requireString(args, "start_time")
            start = if (allDay) {
                cv.put(Events.EVENT_TIMEZONE, "UTC")
                TimeParse.toAllDayUtcStart(raw)
            } else {
                val (ms, tz) = TimeParse.toEpochMs(TimeParse.parse(raw), ToolRegistry.optString(args, "timezone"))
                cv.put(Events.EVENT_TIMEZONE, tz)
                ms
            }
            cv.put(Events.DTSTART, start)
        } else if (allDay != existing.allDay) {
            // 切换全天/非全天必须重算 DTSTART，否则语义错位
            throw ToolError("ARGS_INVALID", "修改 all_day 时必须同时提供 start_time")
        }
        val startMs = start ?: throw ToolError("DEVICE_INTERNAL", "事件缺少开始时间")

        // ---- 时长：三种输入，互斥改写 ----
        // Provider 只在 DTSTART 参与 update 时重算 Instances，故任何时间变更都要把 DTSTART 写回。
        var timeTouchedOnStart = false
        var timeChanged = false
        when {
            hasDuration -> {
                val duration = ToolRegistry.requireString(args, "duration")
                timeChanged = true
                if (effectiveRecurrence != null) {
                    cv.put(Events.DURATION, duration)
                    cv.putNull(Events.DTEND) // 循环事件不能有 DTEND
                } else {
                    cv.put(Events.DTEND, startMs + parseDuration(duration))
                    cv.putNull(Events.DURATION) // 清掉旧的 DURATION，否则 update 冲突
                }
            }
            hasEnd -> {
                if (effectiveRecurrence != null) {
                    throw ToolError("ARGS_INVALID", "循环事件必须用 duration，不能用 end_time")
                }
                val endRaw = ToolRegistry.requireString(args, "end_time")
                val endMs = if (allDay) TimeParse.toAllDayUtcStart(endRaw) else epochOf(TimeParse.parse(endRaw))
                if (endMs <= startMs) throw ToolError("ARGS_INVALID", "end_time 必须晚于 start_time")
                timeChanged = true
                cv.put(Events.DTEND, endMs)
                cv.putNull(Events.DURATION) // 关键：清掉旧的 DURATION
            }
            args.has("start_time") -> {
                // 只改了开始时间：保持原时长，并把时长表示统一到正确的列
                timeChanged = true
                timeTouchedOnStart = true
                val oldLength = existingLengthMs(existing)
                if (oldLength > 0) {
                    if (effectiveRecurrence != null) {
                        if (existing.duration == null) {
                            cv.put(Events.DURATION, formatDuration(oldLength))
                            cv.putNull(Events.DTEND)
                        }
                    } else {
                        cv.put(Events.DTEND, startMs + oldLength)
                        cv.putNull(Events.DURATION)
                    }
                }
            }
        }

        // ---- 改成循环事件：DTEND 必须转成 DURATION ----
        if (newRecurrence != null) {
            cv.put(Events.RRULE, newRecurrence)
            if (existing.rrule == null && existing.duration == null && !hasDuration) {
                val length = existingLengthMs(existing).takeIf { it > 0 }
                    ?: throw ToolError("ARGS_INVALID", "改为循环事件需要已知时长（提供 duration）")
                cv.put(Events.DURATION, formatDuration(length))
                cv.putNull(Events.DTEND)
                timeChanged = true
            }
        }

        // ---- 强制 Instances 重算 + 保住循环规则 ----
        // 实机验证：只写 DTSTART 而不回写 RRULE 时，Provider 会把循环事件退化成单实例。
        // 因此任何时间变更都必须同时写回 RRULE（循环事件）+ DTSTART（触发重算）。
        if (timeChanged) {
            if (effectiveRecurrence != null) {
                cv.put(Events.RRULE, effectiveRecurrence)
            }
            if (!timeTouchedOnStart && !cv.containsKey(Events.DTSTART)) {
                cv.put(Events.DTSTART, startMs)
            }
        }

        // ---- 其它字段 ----
        if (args.has("title")) cv.put(Events.TITLE, ToolRegistry.requireString(args, "title"))
        ToolRegistry.optString(args, "description")?.let { cv.put(Events.DESCRIPTION, it) }
        ToolRegistry.optString(args, "location")?.let { cv.put(Events.EVENT_LOCATION, it) }
        applyAvailability(cv, args)
        return cv
    }

    private fun existingLengthMs(event: ExistingEvent): Long = when {
        event.duration != null -> runCatching { parseDuration(event.duration) }.getOrDefault(0L)
        event.dtend != null && event.dtstart != null -> event.dtend - event.dtstart
        else -> 0L
    }

    private fun applyAvailability(cv: ContentValues, args: JSONObject) {
        when (ToolRegistry.optString(args, "availability")) {
            "FREE" -> cv.put(Events.AVAILABILITY, Events.AVAILABILITY_FREE)
            "TENTATIVE" -> cv.put(Events.AVAILABILITY, Events.AVAILABILITY_TENTATIVE)
            "BUSY" -> cv.put(Events.AVAILABILITY, Events.AVAILABILITY_BUSY)
        }
    }

    /** RFC 5545 时长 → 毫秒（支持 P[n]W/D、T[n]H/M/S）。 */
    private fun parseDuration(spec: String): Long {
        val s = spec.trim().uppercase()
        if (s.length < 2 || s[0] != 'P') {
            throw ToolError("ARGS_INVALID", "duration 必须是 RFC 5545 格式，如 PT30M / PT2H / P1D")
        }
        var i = 1
        var inTime = false
        var total = 0L
        var matched = false
        while (i < s.length) {
            if (s[i] == 'T') {
                inTime = true
                i++
                continue
            }
            val numStart = i
            while (i < s.length && s[i].isDigit()) i++
            if (numStart == i || i >= s.length) {
                throw ToolError("ARGS_INVALID", "duration 格式非法: $spec")
            }
            val n = s.substring(numStart, i).toLong()
            total += when {
                !inTime && s[i] == 'W' -> n * 7 * 86_400_000L
                !inTime && s[i] == 'D' -> n * 86_400_000L
                inTime && s[i] == 'H' -> n * 3_600_000L
                inTime && s[i] == 'M' -> n * 60_000L
                inTime && s[i] == 'S' -> n * 1_000L
                else -> throw ToolError("ARGS_INVALID", "duration 单位非法: $spec")
            }
            i++
            matched = true
        }
        if (!matched || total <= 0) throw ToolError("ARGS_INVALID", "duration 必须大于 0: $spec")
        return total
    }

    /** 毫秒 → RFC 5545 时长（整日为 P{n}D，否则 PT…）。 */
    private fun formatDuration(ms: Long): String {
        val totalSec = ms / 1000
        val days = totalSec / 86_400
        val rest = totalSec % 86_400
        if (rest == 0L && days > 0) return "P${days}D"
        val h = rest / 3600
        val m = (rest % 3600) / 60
        val s = rest % 60
        return buildString {
            append("PT")
            if (h > 0) append("${h}H")
            if (m > 0) append("${m}M")
            if (s > 0 || (h == 0L && m == 0L)) append("${s}S")
        }
    }

    // ---- calendar_delete_event ----

    private fun deleteEvent(ctx: Context, args: JSONObject): JSONObject {
        ensureCalendarPermission(ctx, write = true)
        val eventId = ToolRegistry.requireLong(args, "event_id")
        val occurrence = ToolRegistry.optString(args, "occurrence_start")
        if (occurrence != null) return deleteOccurrence(ctx, eventId, occurrence)

        val cr = ctx.contentResolver
        cr.delete(Reminders.CONTENT_URI, "${Reminders.EVENT_ID}=?", arrayOf(eventId.toString()))
        val deleted = cr.delete(Events.CONTENT_URI, "${Events._ID}=?", arrayOf(eventId.toString()))
        if (deleted == 0) throw ToolError("DEVICE_INTERNAL", "事件不存在: $eventId")
        return JSONObject().put("deleted", true).put("recurring", false)
    }

    /**
     * 删除循环事件的单个实例：往 EXDATE 里追加该次开始时间（RFC 5545 排除日期）。
     *
     * 实机验证（Android 13）：`dtstart + rrule + exdate` 一起 update 才能既重算 Instances
     * 又保留循环规则；只写 exdate 或漏写 rrule 会把整串破坏成单实例。
     */
    private fun deleteOccurrence(ctx: Context, eventId: Long, occurrenceIso: String): JSONObject {
        val existing = loadExistingEvent(ctx, eventId)
        val rrule = existing.rrule
            ?: throw ToolError("ARGS_INVALID", "该事件不是循环事件；删除非循环事件不需要 occurrence_start")
        val occurrenceMs = epochOf(TimeParse.parse(occurrenceIso))

        val newExdate = appendExdate(existing.exdate, occurrenceMs)
        val cv = ContentValues().apply {
            put(Events.DTSTART, existing.dtstart) // 强制 Instances 重算
            put(Events.RRULE, rrule)             // 必须回写，否则循环规则丢失
            put(Events.EXDATE, newExdate)
        }
        ctx.contentResolver.update(Events.CONTENT_URI, cv, "${Events._ID}=?", arrayOf(eventId.toString()))

        // 该实例若有自己的提醒（Reminders 按事件挂，循环事件共享），无需处理
        return JSONObject()
            .put("deleted", true)
            .put("recurring", true)
            .put("excluded_occurrence", TimeParse.format(occurrenceMs, existing.timezone))
    }

    /** EXDATE 是逗号分隔的 UTC 时间列表（yyyyMMdd'T'HHmmss'Z'）；追加且去重。 */
    private fun appendExdate(current: String?, occurrenceMs: Long): String {
        val stamp = java.time.Instant.ofEpochMilli(occurrenceMs)
            .atZone(java.time.ZoneOffset.UTC)
            .format(java.time.format.DateTimeFormatter.ofPattern("yyyyMMdd'T'HHmmss'Z'"))
        val items = (current ?: "").split(",").map { it.trim() }.filter { it.isNotEmpty() }.toMutableList()
        if (stamp !in items) items.add(stamp)
        return items.joinToString(",")
    }


    private fun insertReminders(cr: android.content.ContentResolver, eventId: Long, args: JSONObject) {
        if (!args.has("reminders") || args.isNull("reminders")) return
        val arr = args.optJSONArray("reminders") ?: throw ToolError("ARGS_INVALID", "reminders 必须是数组")
        for (i in 0 until arr.length()) {
            val item = arr.get(i)
            val minutes: Int
            val method: Int
            when (item) {
                is Number -> { minutes = item.toInt(); method = Reminders.METHOD_ALERT }
                is JSONObject -> {
                    minutes = item.optInt("minutes", -1)
                    if (minutes < 0) throw ToolError("ARGS_INVALID", "reminders[$i].minutes 缺失")
                    method = if (item.has("method")) item.getInt("method") else Reminders.METHOD_ALERT
                }
                else -> throw ToolError("ARGS_INVALID", "reminders[$i] 必须是整数或对象")
            }
            val cv = ContentValues().apply {
                put(Reminders.EVENT_ID, eventId)
                put(Reminders.MINUTES, minutes)
                put(Reminders.METHOD, method)
            }
            cr.insert(Reminders.CONTENT_URI, cv) ?: throw ToolError("DEVICE_INTERNAL", "插入提醒失败")
        }
    }

    private fun availabilityName(v: Int): String = when (v) {
        Events.AVAILABILITY_FREE -> "FREE"
        Events.AVAILABILITY_TENTATIVE -> "TENTATIVE"
        else -> "BUSY"
    }

    private fun epochOf(odt: java.time.OffsetDateTime): Long = odt.toInstant().toEpochMilli()
}
