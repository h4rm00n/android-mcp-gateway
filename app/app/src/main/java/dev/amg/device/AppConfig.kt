package dev.amg.device

import android.content.Context
import android.content.SharedPreferences

/**
 * 全局配置与常量。
 *
 * 两种传输：
 *  - `ws` ：wss:// 长连接（TLS 由网关前反代终结）
 *  - `tcp`：裸 TCP + Noise（配对后使用，无应用层协议特征）
 *
 * TOOLSET_VERSION 必须与网关 catalog.py 保持一致。
 */
object AppConfig {
    const val TOOLSET_VERSION = 1
    const val PROTO_VERSION = 1
    const val APP_VERSION = "0.1.0"

    const val TRANSPORT_WS = "ws"
    const val TRANSPORT_TCP = "tcp"

    const val DEFAULT_URL = ""
    const val URL_TEMPLATE = "wss://<host>:<port>/device/ws"
    const val TCP_HOST_TEMPLATE = "<ip 或域名>"
    const val DEFAULT_TOKEN = ""

    private lateinit var prefs: SharedPreferences

    fun init(context: Context) {
        prefs = context.getSharedPreferences("amg", Context.MODE_PRIVATE)
    }

    /** 默认设备名：机型名做清洗（空白转 -，限 32 字符），满足网关 2-32 非空白规则。 */
    fun defaultDeviceName(): String =
        android.os.Build.MODEL
            .trim()
            .replace(Regex("\\s+"), "-")
            .take(32)
            .ifBlank { "android-device" }

    var transport: String
        get() = prefs.getString("transport", TRANSPORT_TCP) ?: TRANSPORT_TCP
        set(v) = prefs.edit().putString("transport", v).apply()

    var url: String
        get() = prefs.getString("url", DEFAULT_URL) ?: DEFAULT_URL
        set(v) = prefs.edit().putString("url", v).apply()

    var token: String
        get() = prefs.getString("token", DEFAULT_TOKEN) ?: DEFAULT_TOKEN
        set(v) = prefs.edit().putString("token", v).apply()

    var deviceName: String
        get() = prefs.getString("device_name", null) ?: defaultDeviceName()
        set(v) = prefs.edit().putString("device_name", v).apply()

    // ---- 配对（TCP 传输用）----

    var pairingHost: String
        get() = prefs.getString("pair_host", "") ?: ""
        set(v) = prefs.edit().putString("pair_host", v).apply()

    var pairingPort: String
        get() = prefs.getString("pair_port", "18438") ?: "18438"
        set(v) = prefs.edit().putString("pair_port", v).apply()

    /** 启动前自检：返回错误描述，null 表示可连接。 */
    fun validate(): String? {
        if (deviceName.isBlank() || deviceName.length > 32) return "设备名必须是 1-32 个字符"
        return when (transport) {
            TRANSPORT_TCP -> {
                val paired = PairingStore.load(globalContext)
                when {
                    paired == null -> "未配对：请填配对码并点「配对」"
                    paired.host.isBlank() -> "已配对但网关地址为空"
                    else -> null
                }
            }
            else -> if (url.isBlank()) "未填写网关地址（格式 $URL_TEMPLATE）" else null
        }
    }

    /** 供 validate() 读取 PairingStore 使用（在 init 时注入）。 */
    private lateinit var globalContext: Context

    fun bindContext(context: Context) {
        globalContext = context.applicationContext
    }
}
