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
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.ScheduledFuture
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import kotlin.math.min
import kotlin.math.pow
import kotlin.random.Random

/**
 * 传输抽象：WS(wss) 与裸 TCP(Noise) 共用同一套会话逻辑。
 *
 * 所有实现都必须满足：open() 建立连接（可阻塞）、receive() 阻塞读一帧、
 * 对端关闭时 receive() 抛 Closed、send() 线程安全。
 */
interface DeviceLink {
    fun open()
    fun send(text: String)
    fun receive(): String
    fun close()

    class Closed(message: String = "连接已关闭") : Exception(message)
}

/** OkHttp WebSocket 实现（wss:// 路径）。 */
class WsLink(private val url: String, private val token: String) : DeviceLink {
    private val client = OkHttpClient()
    private val incoming = LinkedBlockingQueue<Any>()
    private var socket: WebSocket? = null

    private val opened = CountDownLatch(1)
    @Volatile private var openError: Throwable? = null

    private val listener = object : WebSocketListener() {
        override fun onOpen(webSocket: WebSocket, response: Response) {
            android.util.Log.i("AMG", "ws onOpen code=${response.code}")
            socket = webSocket
            opened.countDown()
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            incoming.put(text)
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            android.util.Log.w("AMG", "ws onFailure: ${t.message ?: t.javaClass.name}")
            openError = t
            opened.countDown()
            incoming.put(DeviceLink.Closed(t.message ?: t.javaClass.simpleName))
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
            incoming.put(DeviceLink.Closed("关闭 code=$code $reason"))
        }
    }

    override fun open() {
        val request = Request.Builder().url(url)
            .header("Authorization", "Bearer $token")
            .build()
        client.newWebSocket(request, listener)
        if (!opened.await(15, TimeUnit.SECONDS)) throw DeviceLink.Closed("连接超时")
        openError?.let { throw DeviceLink.Closed(it.message ?: "连接失败") }
    }

    override fun send(text: String) {
        val ws = socket ?: throw DeviceLink.Closed()
        if (!ws.send(text)) throw DeviceLink.Closed("发送失败")
    }

    override fun receive(): String {
        val item = incoming.take()
        if (item is DeviceLink.Closed) throw item
        return item as String
    }

    override fun close() {
        runCatching { socket?.close(1000, "client stop") }
    }
}

/** 裸 TCP + Noise 实现。 */
class TcpLink(
    private val paired: PairedGateway,
    private val devName: String,
    private val appContext: android.content.Context,
) : DeviceLink {
    private var session: NoiseSession? = null
    private val writeLock = Any()

    override fun open() {
        session = NoiseClient.connect(appContext, paired, devName)
    }

    override fun send(text: String) {
        val s = session ?: throw DeviceLink.Closed()
        synchronized(writeLock) { s.sendText(text) }
    }

    override fun receive(): String {
        val s = session ?: throw DeviceLink.Closed()
        return try {
            s.receiveText()
        } catch (e: Exception) {
            throw DeviceLink.Closed(e.message ?: e.javaClass.simpleName)
        }
    }

    override fun close() {
        runCatching { session?.close() }
    }
}

/**
 * 设备 Agent 前台服务：传输无关的连接管理（心跳 / 退避重连 / tool_call 分发）。
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
        AppConfig.bindContext(this)
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

/**
 * 连接与协议封装。读循环跑在独立线程，工具串行执行（协议：并发固定 1）。
 */
class AgentConnection(
    private val appContext: android.content.Context,
    private val onState: (String) -> Unit,
) {
    private val readExecutor = Executors.newSingleThreadExecutor()
    private val toolExecutor = Executors.newSingleThreadExecutor()
    private val scheduler = Executors.newSingleThreadScheduledExecutor()
    private val idCounter = AtomicInteger(0)

    @Volatile private var link: DeviceLink? = null
    @Volatile private var missedAcks = 0
    @Volatile private var manualClose = false
    @Volatile private var fatal = false
    @Volatile private var connected = false
    @Volatile private var heartbeatFuture: ScheduledFuture<*>? = null
    @Volatile private var reconnectAttempts = 0
    @Volatile private var reconnectFuture: ScheduledFuture<*>? = null

    fun start() {
        manualClose = false
        fatal = false
        connectAsync()
    }

    fun stop() {
        manualClose = true
        stopHeartbeat()
        reconnectFuture?.cancel(false)
        link?.close()
    }

    // ---- 连接建立 ----

    private fun connectAsync() {
        if (manualClose || fatal) return
        val problem = AppConfig.validate()
        if (problem != null) {
            onState("✗ $problem")
            fatal = true
            return
        }
        onState("连接中（${AppConfig.transport}）")
        readExecutor.submit { connectAndRun() }
    }

    private fun connectAndRun() {
        if (manualClose || fatal) return
        val newLink = try {
            buildLink()
        } catch (e: Exception) {
            onState("✗ ${e.message}")
            fatal = true
            return
        }

        try {
            newLink.open()
        } catch (e: Exception) {
            onState("连接失败: ${e.message ?: e.javaClass.simpleName}")
            runCatching { newLink.close() }
            if (!manualClose && !fatal) scheduleReconnect()
            return
        }

        link = newLink
        connected = true
        missedAcks = 0
        onState("已连接，等待注册")
        // 连接稳定 60s 后重置退避（协议 §8）
        scheduler.schedule({ reconnectAttempts = 0 }, 60, TimeUnit.SECONDS)
        sendHello()
        startHeartbeat()

        try {
            while (true) {
                handleFrame(newLink.receive())
            }
        } catch (e: DeviceLink.Closed) {
            if (!manualClose && !fatal) {
                onState("连接断开: ${e.message}")
            }
        } catch (e: Exception) {
            android.util.Log.w("AMG", "read loop error", e)
        } finally {
            stopHeartbeat()
            connected = false
            link = null
            runCatching { newLink.close() }
            if (!manualClose && !fatal) scheduleReconnect()
        }
    }

    private fun buildLink(): DeviceLink = when (AppConfig.transport) {
        AppConfig.TRANSPORT_TCP -> {
            val paired = PairingStore.load(appContext)
                ?: throw IllegalStateException("未配对：请先在界面输入配对码完成配对")
            TcpLink(paired, AppConfig.deviceName, appContext)
        }
        else -> WsLink(AppConfig.url, AppConfig.token)
    }

    // ---- 协议帧 ----

    private fun sendHello() {
        val payload = JSONObject()
            .put("proto_version", AppConfig.PROTO_VERSION)
            .put("device_id", AppConfig.deviceName)
            .put("toolset_version", AppConfig.TOOLSET_VERSION)
            .put(
                "device", JSONObject()
                    .put("model", Build.MODEL)
                    .put("android", Build.VERSION.SDK_INT)
                    .put("app_version", AppConfig.APP_VERSION)
                    .put("capabilities", JSONArray().put("calendar").put("notifications"))
            )
        sendFrame("hello", payload)
    }

    private fun sendFrame(type: String, payload: JSONObject, id: String? = null) {
        val frame = JSONObject()
            .put("type", type)
            .put("id", id ?: "${type.first()}-${idCounter.incrementAndGet()}")
            .put("ts", System.currentTimeMillis())
            .put("payload", payload)
        try {
            link?.send(frame.toString())
        } catch (e: Exception) {
            android.util.Log.w("AMG", "send failed: ${e.message}")
        }
    }

    private fun handleFrame(text: String) {
        val msg = try {
            JSONObject(text)
        } catch (e: Exception) {
            return // 非法帧：交给读循环异常路径处理
        }
        when (val type = msg.optString("type")) {
            "hello_ack" -> {
                val hb = msg.optJSONObject("payload")?.optLong("heartbeat_interval_ms", 30000) ?: 30000
                onState("注册成功 ✓ 心跳 ${hb}ms")
            }
            "heartbeat_ack" -> missedAcks = 0
            "tool_call" -> dispatchToolCall(
                msg.optString("id"),
                msg.optJSONObject("payload") ?: JSONObject()
            )
            "tool_cancel" -> Unit // 预留（协议 §7）
            "error" -> {
                val p = msg.optJSONObject("payload")
                val code = p?.optString("code") ?: "?"
                onState("网关错误: $code ${p?.optString("message")}")
                when (code) {
                    "TOOLSET_MISMATCH" -> fatal = true
                    "NAME_IN_USE" -> {
                        fatal = true
                        onState("⚠ 设备名被占用，请在界面改名后重新启动服务")
                    }
                }
            }
            else -> sendFrame(
                "error",
                JSONObject().put("code", "PROTOCOL_ERROR").put("message", "未知消息类型: $type"),
                msg.optString("id")
            )
        }
    }

    // ---- tool_call → 执行器（串行）----

    private fun dispatchToolCall(callId: String, payload: JSONObject) {
        val tool = payload.optString("tool")
        val args = payload.optJSONObject("args") ?: JSONObject()
        toolExecutor.submit {
            val result = try {
                JSONObject().put("ok", true)
                    .put("result", ToolRegistry.execute(appContext, tool, args))
            } catch (e: ToolError) {
                JSONObject().put("ok", false)
                    .put("error", JSONObject().put("code", e.code).put("message", e.message ?: ""))
            } catch (e: Exception) {
                JSONObject().put("ok", false)
                    .put("error", JSONObject().put("code", "DEVICE_INTERNAL")
                        .put("message", e.message ?: e.javaClass.simpleName))
            }
            sendFrame("tool_result", result, callId)
        }
    }

    // ---- 心跳（协议 §5：连续 2 次无 ack → 断开）----

    private fun startHeartbeat() {
        stopHeartbeat()
        heartbeatFuture = scheduler.scheduleWithFixedDelay({
            try {
                if (missedAcks >= 2) {
                    onState("心跳无应答，重连")
                    link?.close()
                    return@scheduleWithFixedDelay
                }
                sendFrame("heartbeat", JSONObject())
                missedAcks++
            } catch (t: Throwable) {
                // 读循环会接管
            }
        }, 30_000, 30_000, TimeUnit.MILLISECONDS)
    }

    private fun stopHeartbeat() {
        heartbeatFuture?.cancel(false)
        heartbeatFuture = null
    }

    // ---- 指数退避重连（协议 §8）----

    private fun scheduleReconnect() {
        if (manualClose || fatal) return
        val base = min(60.0, 1.0 * 2.0.pow(reconnectAttempts))
        val delayMs = (base * 1000 * (0.7 + Random.nextDouble() * 0.6)).toLong()
        reconnectAttempts++
        onState("断开，${"%.1f".format(delayMs / 1000.0)}s 后重连（第 $reconnectAttempts 次）")
        reconnectFuture?.cancel(false)
        reconnectFuture = scheduler.schedule({ connectAsync() }, delayMs, TimeUnit.MILLISECONDS)
    }
}
