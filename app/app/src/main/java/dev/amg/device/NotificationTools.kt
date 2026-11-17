package dev.amg.device

import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import org.json.JSONArray
import org.json.JSONObject

/** NotificationListenerService 与工具执行器之间的桥（服务连接由系统管理）。 */
object NotificationBridge {
    @Volatile
    var listener: AgentNotificationListener? = null
}

class AgentNotificationListener : NotificationListenerService() {

    override fun onListenerConnected() {
        NotificationBridge.listener = this
    }

    override fun onListenerDisconnected() {
        if (NotificationBridge.listener === this) NotificationBridge.listener = null
        // 请求系统重连（API 24+）
        requestRebind(android.content.ComponentName(this, AgentNotificationListener::class.java))
    }

    override fun onDestroy() {
        if (NotificationBridge.listener === this) NotificationBridge.listener = null
        super.onDestroy()
    }
}

/** 通知工具×3：read / dismiss（经 NotificationListener），post（自身名义）。 */
object NotificationTools {

    const val CHANNEL_ID = "amg_agent"

    fun register() {
        ToolRegistry.register("notification_read", ::read)
        ToolRegistry.register("notification_dismiss", ::dismiss)
        ToolRegistry.register("notification_post", ::post)
    }

    private fun listener(): AgentNotificationListener =
        NotificationBridge.listener ?: throw ToolError("PERMISSION_DENIED", "通知使用权未授予（设置 → 通知使用权）")

    private fun ensurePostPermission(ctx: Context) {
        if (ctx.checkSelfPermission("android.permission.POST_NOTIFICATIONS") !=
            android.content.pm.PackageManager.PERMISSION_GRANTED
        ) {
            throw ToolError("PERMISSION_DENIED", "POST_NOTIFICATIONS 未授予")
        }
    }

    // ---- notification_read ----

    /**
     * 读取通知栏快照。
     *
     * 字段补全：很多 App（尤其聊天/邮件）只把内容放在 MessagingStyle 的 conversation
     * 或 InboxStyle 的多行里，EXTRA_TEXT 是空的——只读 EXTRA_TEXT 会得到一堆空通知。
     * 这里按 BIG_TEXT → MessagingStyle 会话 → InboxStyle 行 → TEXT → ticker 的顺序回退。
     */
    private fun read(ctx: Context, args: JSONObject): JSONObject {
        val sbns: Array<StatusBarNotification> = listener().activeNotifications ?: emptyArray()
        val pkgFilter = ToolRegistry.optString(args, "package_name")
        val limit = ToolRegistry.optLong(args, "limit")?.toInt() ?: 20
        val includeOngoing = ToolRegistry.optBool(args, "include_ongoing", default = true)
        val nonEmptyOnly = ToolRegistry.optBool(args, "non_empty_only", default = false)

        val out = JSONArray()
        var skippedEmpty = 0
        sbns.sortedByDescending { it.postTime }
            .filter { pkgFilter == null || it.packageName == pkgFilter }
            .filter { includeOngoing || it.notification.flags and android.app.Notification.FLAG_ONGOING_EVENT == 0 }
            .forEach { sbn ->
                if (out.length() >= limit) return@forEach
                val extras = sbn.notification.extras
                val title = extras.getCharSequence(android.app.Notification.EXTRA_TITLE)?.toString()?.trim() ?: ""
                val text = extractText(sbn).trim()
                if (nonEmptyOnly && title.isEmpty() && text.isEmpty()) {
                    skippedEmpty++
                    return@forEach
                }
                val ongoing = sbn.notification.flags and android.app.Notification.FLAG_ONGOING_EVENT != 0
                val row = JSONObject()
                    .put("key", sbn.key)
                    .put("package", sbn.packageName)
                    .put("title", title)
                    .put("text", text)
                    .put("post_time", sbn.postTime)
                    .put("ongoing", ongoing)
                extras.getCharSequence(android.app.Notification.EXTRA_SUB_TEXT)?.toString()?.trim()
                    ?.takeIf { it.isNotEmpty() }?.let { row.put("sub_text", it) }
                extras.getCharSequence(android.app.Notification.EXTRA_CONVERSATION_TITLE)?.toString()
                    ?.trim()?.takeIf { it.isNotEmpty() }?.let { row.put("conversation_title", it) }
                // 分类：聊天/邮件/短信等，便于 AI 判断内容类型
                sbn.notification.category?.let { row.put("category", it) }
                // App 显示名（让 AI 不必认识包名）
                appLabel(ctx, sbn.packageName)?.let { row.put("app", it) }
                out.put(row)
            }
        val result = JSONObject().put("notifications", out).put("count", out.length())
        if (skippedEmpty > 0) result.put("skipped_empty", skippedEmpty)
        return result
    }

    /** 通知正文的多级回退提取。 */
    private fun extractText(sbn: StatusBarNotification): String {
        val n = sbn.notification
        val extras = n.extras

        extras.getCharSequence(android.app.Notification.EXTRA_BIG_TEXT)?.toString()?.takeIf { it.isNotBlank() }
            ?.let { return it }

        // MessagingStyle（微信/短信等聊天类）：内容在 EXTRA_MESSAGES 的 Bundle 数组里，
        // 平台侧没有公开的提取 API（extractMessagingStyleFromNotification 是 @hide），手动解析。
        extractMessages(extras)?.let { return it }

        // InboxStyle：多行摘要
        val inbox = extras.getCharSequenceArray(android.app.Notification.EXTRA_TEXT_LINES)
        if (inbox != null && inbox.isNotEmpty()) {
            return inbox.mapNotNull { it?.toString()?.takeIf(String::isNotBlank) }.takeLast(3).joinToString(" / ")
        }

        extras.getCharSequence(android.app.Notification.EXTRA_TEXT)?.toString()?.takeIf { it.isNotBlank() }
            ?.let { return it }

        n.tickerText?.toString()?.takeIf { it.isNotBlank() }?.let { return it }
        return ""
    }

    /**
     * 从 EXTRA_MESSAGES 取最近几条消息正文。
     * 每项是 Bundle：`text`(CharSequence) / `time`(long) / `sender`(CharSequence) / `sender_person`(Person)。
     */
    private fun extractMessages(extras: android.os.Bundle): String? {
        val raw = extras.getParcelableArray(android.app.Notification.EXTRA_MESSAGES) ?: return null
        val lines = raw.mapNotNull { item ->
            val b = item as? android.os.Bundle ?: return@mapNotNull null
            val text = b.getCharSequence("text")?.toString()?.trim()?.takeIf { it.isNotEmpty() }
                ?: return@mapNotNull null
            text
        }
        if (lines.isEmpty()) return null
        return lines.takeLast(3).joinToString(" / ")
    }

    /** 包名 → App 显示名（取不到就返回 null，不阻塞）。 */
    private fun appLabel(ctx: Context, packageName: String): String? = runCatching {
        val info = ctx.packageManager.getApplicationInfo(packageName, 0)
        ctx.packageManager.getApplicationLabel(info).toString()
    }.getOrNull()

    // ---- notification_dismiss ----

    private fun dismiss(ctx: Context, args: JSONObject): JSONObject {
        val key = ToolRegistry.requireString(args, "notification_key")
        try {
            listener().cancelNotification(key)
            return JSONObject().put("dismissed", true)
        } catch (e: IllegalArgumentException) {
            throw ToolError("DEVICE_INTERNAL", "通知不存在或已消失: ${e.message}")
        } catch (e: SecurityException) {
            throw ToolError("PERMISSION_DENIED", "通知使用权未授予或已失效")
        }
    }

    // ---- notification_post ----

    private fun post(ctx: Context, args: JSONObject): JSONObject {
        ensurePostPermission(ctx)
        val nm = ctx.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, "Agent", NotificationManager.IMPORTANCE_DEFAULT)
        )
        val title = ToolRegistry.requireString(args, "title")
        val body = ToolRegistry.optString(args, "body") ?: ""
        val notification = android.app.Notification.Builder(ctx, CHANNEL_ID)
            .setContentTitle(title)
            .setContentText(body)
            .setStyle(android.app.Notification.BigTextStyle().bigText(body))
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setAutoCancel(true)
            .build()
        nm.notify((System.currentTimeMillis() % 100000).toInt(), notification)
        return JSONObject().put("posted", true)
    }
}
