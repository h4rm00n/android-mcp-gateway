package dev.amg.device

import android.content.Context
import android.content.SharedPreferences

/**
 * 全局配置与常量。
 *
 * token = 统一配对凭证（只管准入）；身份 = 自报 deviceName（在线唯一，协议 §2/§6）。
 * TOOLSET_VERSION 必须与网关 catalog.py 保持一致——任一侧修改工具 schema/语义，两侧同时 bump。
 */
object AppConfig {
    const val TOOLSET_VERSION = 1
    const val PROTO_VERSION = 1
    const val APP_VERSION = "0.1.0"

    /**
     * 网关地址不预设默认值——必须由用户填写实际可达地址。
     * [URL_TEMPLATE] 仅作为输入框提示，说明地址结构；`ws://` 与 `wss://` 都支持
     * （TLS 由网关前的反代终结，App 不参与证书判断）。
     */
    const val DEFAULT_URL = ""
    const val URL_TEMPLATE = "ws://<ip>:<port>/device/ws"
    const val DEFAULT_TOKEN = ""

    private lateinit var prefs: SharedPreferences

    fun init(context: Context) {
        prefs = context.getSharedPreferences("amg", Context.MODE_PRIVATE)
    }

    /** 默认设备名：机型名做清洗（空白转 -，限 32 字符），保证满足网关的 2-32 非空白规则。 */
    fun defaultDeviceName(): String =
        android.os.Build.MODEL
            .trim()
            .replace(Regex("\\s+"), "-")
            .take(32)
            .ifBlank { "android-device" }

    var url: String
        get() = prefs.getString("url", DEFAULT_URL) ?: DEFAULT_URL
        set(v) = prefs.edit().putString("url", v).apply()

    var token: String
        get() = prefs.getString("token", DEFAULT_TOKEN) ?: DEFAULT_TOKEN
        set(v) = prefs.edit().putString("token", v).apply()

    var deviceName: String
        get() = prefs.getString("device_name", null) ?: defaultDeviceName()
        set(v) = prefs.edit().putString("device_name", v).apply()
}
