package dev.amg.device

import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Typeface
import android.os.Bundle
import android.provider.Settings
import android.view.Gravity
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView

/**
 * 配置界面（MVP）：网关 URL / token → 启停前台服务；运行时权限引导。
 */
class MainActivity : Activity() {

    private lateinit var urlInput: EditText
    private lateinit var tokenInput: EditText
    private lateinit var nameInput: EditText
    private lateinit var statusView: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        AppConfig.init(this)
        CalendarTools.register()
        NotificationTools.register()
        DeviceTools.register()

        setContentView(buildUi())
        urlInput.setText(AppConfig.url)
        tokenInput.setText(AppConfig.token)

        GatewayService.onStatus = { s -> statusView.append("$s\n") }
        statusView.text = if (GatewayService.isRunning) "服务运行中\n" else ""

        // 自动启动：打开 App 即连接（前台上下文内启动，满足 FGS 限制）
        if (!GatewayService.isRunning && urlInput.text.isNotBlank() && tokenInput.text.isNotBlank()) {
            startForegroundService(Intent(this, GatewayService::class.java))
            statusView.append("服务已启动\n")
        }

        requestPermissions(
            arrayOf(
                "android.permission.READ_CALENDAR",
                "android.permission.WRITE_CALENDAR",
                "android.permission.POST_NOTIFICATIONS",
            ),
            1,
        )
    }

    override fun onDestroy() {
        if (GatewayService.onStatus != null) GatewayService.onStatus = null
        super.onDestroy()
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        val denied = permissions.filterIndexed { i, _ -> grantResults[i] != PackageManager.PERMISSION_GRANTED }
        statusView.append(if (denied.isEmpty()) "权限已授予 ✓\n" else "未授予: ${denied.joinToString()}\n")
    }

    private fun buildUi(): ScrollView {
        fun label(text: String): TextView = TextView(this).apply {
            this.text = text
            setPadding(0, dp(12), 0, dp(4))
        }

        urlInput = EditText(this).apply {
            setText(AppConfig.url)
            hint = AppConfig.URL_TEMPLATE
            textSize = 13f
        }
        tokenInput = EditText(this).apply { setText(AppConfig.token); textSize = 13f }
        nameInput = EditText(this).apply { setText(AppConfig.deviceName); textSize = 13f }

        statusView = TextView(this).apply {
            textSize = 12f
            typeface = Typeface.MONOSPACE
            setTextColor(0xFF33691E.toInt())
            setPadding(0, dp(16), 0, 0)
        }

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(16), dp(24), dp(16), dp(16))
        }

        val buttonRow = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        fun addToRow(b: Button) {
            b.layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f).apply {
                setMargins(dp(4), 0, dp(4), 0)
            }
            buttonRow.addView(b)
        }
        addToRow(Button(this).apply {
            text = "保存并启动"
            setOnClickListener {
                AppConfig.url = urlInput.text.toString().trim()
                AppConfig.token = tokenInput.text.toString().trim()
                AppConfig.deviceName = nameInput.text.toString().trim()
                startForegroundService(Intent(this@MainActivity, GatewayService::class.java))
                statusView.append("服务已启动\n")
            }
        })
        addToRow(Button(this).apply {
            text = "停止"
            setOnClickListener {
                stopService(Intent(this@MainActivity, GatewayService::class.java))
                statusView.append("服务已停止\n")
            }
        })

        root.addView(TextView(this).apply {
            text = "Android MCP Gateway 设备端"
            textSize = 18f
            gravity = Gravity.CENTER
        })
        root.addView(label("网关地址（ws:// 或 wss://，端口 + /device/ws）"))
        root.addView(urlInput)
        root.addView(label("配对 token（网关统一发放）"))
        root.addView(tokenInput)
        root.addView(label("设备名称（登记身份，在线唯一）"))
        root.addView(nameInput)
        root.addView(buttonRow)
        root.addView(Button(this).apply {
            text = "授予日历/通知运行时权限（弹窗）"
            setOnClickListener {
                requestPermissions(
                    arrayOf(
                        "android.permission.READ_CALENDAR",
                        "android.permission.WRITE_CALENDAR",
                        "android.permission.POST_NOTIFICATIONS",
                    ),
                    1,
                )
            }
        })
        root.addView(Button(this).apply {
            text = "通知使用权设置（需手动开启）"
            setOnClickListener {
                startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS))
            }
        })
        root.addView(statusView)
        return ScrollView(this).apply { addView(root) }
    }

    private fun dp(v: Int): Int = (v * resources.displayMetrics.density).toInt()
}
