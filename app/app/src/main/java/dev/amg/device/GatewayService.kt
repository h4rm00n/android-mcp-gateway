package dev.amg.device

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import org.json.JSONArray
import org.json.JSONObject
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledFuture
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import kotlin.math.min
import kotlin.math.pow
import kotlin.random.Random

/**
 * 设备 Agent 前台服务：WSS 出站长连接 + 心跳 + 指数退避重连 + tool_call 分发（协议 §2-§8）。
 */
class GatewayService : Service() {

    companion object {
        @Volatile var onStatus: ((String) -> Unit)? = null
        @Volatile var isRunning = false
    }

    private var connection: AgentConnection? = null
    private val mainHandler = Handler(Looper.getMainLooper())

    override fun onCreate() {
        super.onCreate()
        AppConfig.init(this)
        CalendarTools.register()
        NotificationTools.register()
        DeviceTools.register()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        isRunning = true
        startAsForeground()
        if (connection == null) {
            connection = AgentConnection(applicationContext) { s -> mainHandler.post { onStatus?.invoke(s) } }
            connection?.start()
        }
        return START_STICKY
    }

    override fun onDestroy() {
        isRunning = false
        connection?.stop()
        connection = null
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun startAsForeground() {
        val nm = getSystemService(NOTIFICATION_SERVICE) as NotificationManager
        nm.createNotificationChannel(
            NotificationChannel("amg_service", "网关连接", NotificationManager.IMPORTANCE_LOW)
        )
        val notification: Notification = Notification.Builder(this, "amg_service")
            .setContentTitle("Android MCP Gateway")
            .setContentText("设备 Agent 运行中")
            .setSmallIcon(android.R.drawable.ic_popup_reminder)
            .setOngoing(true)
            .build()
        if (Build.VERSION.SDK_INT >= 29) {
            startForeground(1, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        } else {
            startForeground(1, notification)
        }
    }
}

/** WS 连接与协议封装。串行执行工具（协议：并发固定 1）。 */
class AgentConnection(
    private val appContext: android.content.Context,
    private val onState: (String) -> Unit,
) {
    private val client = OkHttpClient()
    private var ws: WebSocket? = null
    private val executor = Executors.newSingleThreadExecutor()
    private val scheduler = Executors.newSingleThreadScheduledExecutor()
    private val idCounter = AtomicInteger(0)

    @Volatile private var heartbeatIntervalMs = 30_000L
    @Volatile private var missedAcks = 0
    @Volatile private var manualClose = false
    @Volatile private var fatal = false // TOOLSET_MISMATCH 等：停止自动重连，等人工介入
    @Volatile private var heartbeatFuture: ScheduledFuture<*>? = null
    private var reconnectAttempts = 0
    private var reconnectFuture: ScheduledFuture<*>? = null

    fun start() {
        manualClose = false
        fatal = false
        connect()
    }

    fun stop() {
        manualClose = true
        stopHeartbeat()
        reconnectFuture?.cancel(false)
        ws?.close(1000, "client stop")
    }

    private fun connect() {
        if (manualClose || fatal) return
        if (AppConfig.url.isBlank()) {
            onState("✗ 未填写网关地址（格式 ${AppConfig.URL_TEMPLATE}）")
            fatal = true // 等用户在界面填写后重新启动服务
            return
        }
        onState("连接中 ${AppConfig.url}")
        android.util.Log.i("AMG", "connect ${AppConfig.url} (token ${AppConfig.token.length} chars)")
        val request = Request.Builder()
            .url(AppConfig.url)
            .header("Authorization", "Bearer ${AppConfig.token}")
            .build()
        ws = client.newWebSocket(request, listener)
    }

    private val listener = object : WebSocketListener() {
        override fun onOpen(webSocket: WebSocket, response: Response) {
            android.util.Log.i("AMG", "onOpen code=${response.code}")
            missedAcks = 0
            sendHello(webSocket)
            onState("已连接，等待注册")
            // 连接稳定 60s 后重置退避（协议 §8）
            scheduler.schedule({ reconnectAttempts = 0 }, 60, TimeUnit.SECONDS)
            startHeartbeat()
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            handleFrame(webSocket, text)
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            if (manualClose || fatal) return
            android.util.Log.w("AMG", "onFailure: ${t.message ?: t.javaClass.name}", t)
            onState("连接失败: ${t.message ?: t.javaClass.simpleName}")
            scheduleReconnect()
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
            if (manualClose || fatal) return
            onState("连接关闭 code=$code $reason")
            scheduleReconnect()
        }
    }

    // ---- 协议帧 ----

    private fun sendHello(webSocket: WebSocket) {
        val payload = JSONObject()
            .put("proto_version", AppConfig.PROTO_VERSION)
            .put("device_id", AppConfig.deviceName) // 自报身份，在线唯一（协议 §6）
            .put("toolset_version", AppConfig.TOOLSET_VERSION)
            .put(
                "device", JSONObject()
                    .put("model", Build.MODEL)
                    .put("android", Build.VERSION.SDK_INT)
                    .put("app_version", AppConfig.APP_VERSION)
                    .put("capabilities", JSONArray().put("calendar").put("notifications"))
            )
        send(webSocket, "hello", payload)
    }

    private fun send(webSocket: WebSocket, type: String, payload: JSONObject, id: String? = null) {
        val frame = JSONObject()
            .put("type", type)
            .put("id", id ?: "${type.first()}-${idCounter.incrementAndGet()}")
            .put("ts", System.currentTimeMillis())
            .put("payload", payload)
        webSocket.send(frame.toString())
    }

    private fun handleFrame(webSocket: WebSocket, text: String) {
        val msg = try {
            JSONObject(text)
        } catch (e: Exception) {
            webSocket.close(4000, "PROTOCOL_ERROR")
            return
        }
        when (val type = msg.optString("type")) {
            "hello_ack" -> onState("注册成功 ✓ 心跳 ${msg.optJSONObject("payload")?.optLong("heartbeat_interval_ms", 30000) ?: 30000}ms")
            "heartbeat_ack" -> missedAcks = 0
            "tool_call" -> dispatchToolCall(webSocket, msg.optString("id"), msg.optJSONObject("payload") ?: JSONObject())
            "tool_cancel" -> Unit // 预留：MVP 网关不转发取消（协议 §7）
            "error" -> {
                val p = msg.optJSONObject("payload")
                val code = p?.optString("code") ?: "?"
                onState("网关错误: $code ${p?.optString("message")}")
                when (code) {
                    "TOOLSET_MISMATCH" -> fatal = true // 不原样重连重发（协议 §6 防死循环）
                    "NAME_IN_USE" -> { // 在线同名：改名前不得重试（协议 §2 防互踢死循环）
                        fatal = true
                        onState("⚠ 设备名被占用，请在界面改名后重新启动服务")
                    }
                }
            }
            else -> {
                send(
                    webSocket, "error", JSONObject()
                        .put("code", "PROTOCOL_ERROR")
                        .put("message", "未知消息类型: $type"),
                    msg.optString("id")
                )
            }
        }
    }

    // ---- tool_call → 执行器（串行，协议 §4）----

    private fun dispatchToolCall(webSocket: WebSocket, callId: String, payload: JSONObject) {
        val tool = payload.optString("tool")
        val args = payload.optJSONObject("args") ?: JSONObject()
        executor.submit {
            val result = try {
                val value = ToolRegistry.execute(appContext, tool, args)
                JSONObject().put("ok", true).put("result", value)
            } catch (e: ToolError) {
                JSONObject().put("ok", false)
                    .put("error", JSONObject().put("code", e.code).put("message", e.message ?: ""))
            } catch (e: Exception) {
                JSONObject().put("ok", false)
                    .put("error", JSONObject().put("code", "DEVICE_INTERNAL").put("message", e.message ?: e.javaClass.simpleName))
            }
            send(webSocket, "tool_result", result, callId)
        }
    }

    // ---- 心跳（协议 §5：连续 2 次未收到 ack → 断开重连）----

    private fun startHeartbeat() {
        stopHeartbeat()
        heartbeatIntervalMs = 30_000L // hello_ack 的 interval 到达前用默认值
        heartbeatFuture = scheduler.scheduleWithFixedDelay({
            try {
                if (missedAcks >= 2) {
                    onState("心跳无应答，重连")
                    ws?.close(4000, "heartbeat timeout")
                    return@scheduleWithFixedDelay
                }
                ws?.let { send(it, "heartbeat", JSONObject()) }
                missedAcks++
            } catch (t: Throwable) {
                // 连接已死，onFailure 会接管
            }
        }, heartbeatIntervalMs, heartbeatIntervalMs, TimeUnit.MILLISECONDS)
    }

    private fun stopHeartbeat() {
        heartbeatFuture?.cancel(false)
        heartbeatFuture = null
    }

    // ---- 指数退避重连（协议 §8：1s 起 ×2 上限 60s，±30% 抖动）----

    private fun scheduleReconnect() {
        if (manualClose || fatal) return
        stopHeartbeat()
        val base = min(60.0, 1.0 * 2.0.pow(reconnectAttempts))
        val delayMs = (base * 1000 * (0.7 + Random.nextDouble() * 0.6)).toLong()
        reconnectAttempts++
        onState("断开，${"%.1f".format(delayMs / 1000.0)}s 后重连（第 $reconnectAttempts 次）")
        reconnectFuture = scheduler.schedule({ connect() }, delayMs, TimeUnit.MILLISECONDS)
    }
}
