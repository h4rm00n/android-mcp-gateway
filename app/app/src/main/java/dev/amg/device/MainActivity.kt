package dev.amg.device

import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Typeface
import android.os.Bundle
import android.provider.Settings
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.RadioButton
import android.widget.RadioGroup
import android.widget.ScrollView
import android.widget.TextView
import java.util.concurrent.Executors

/**
 * 配置界面：传输方式（WS / 裸 TCP+Noise）、地址、配对、设备名、权限引导。
 */
class MainActivity : Activity() {

    private lateinit var wsGroup: LinearLayout
    private lateinit var tcpGroup: LinearLayout
    private lateinit var urlInput: EditText
    private lateinit var tokenInput: EditText
    private lateinit var pairHostInput: EditText
    private lateinit var pairPortInput: EditText
    private lateinit var pairCodeInput: EditText
    private lateinit var pairStatusView: TextView
    private lateinit var nameInput: EditText
    private lateinit var statusView: TextView

    private val io = Executors.newSingleThreadExecutor()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        AppConfig.init(this)
        AppConfig.bindContext(this)
        CalendarTools.register()
        NotificationTools.register()
        DeviceTools.register()

        setContentView(buildUi())
        applyTransportVisibility(AppConfig.transport)

        GatewayService.onStatus = { s -> runOnUiThread { statusView.append("$s\n") } }
        statusView.text = if (GatewayService.isRunning) "服务运行中\n" else ""
        refreshPairingStatus()

        // 自动启动：已有可用配置时直接连
        if (!GatewayService.isRunning && AppConfig.validate() == null) {
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
        GatewayService.onStatus = null
        io.shutdown()
        super.onDestroy()
    }

    override fun onRequestPermissionsResult(
        requestCode: Int, permissions: Array<out String>, grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        val denied = permissions.filterIndexed { i, _ -> grantResults[i] != PackageManager.PERMISSION_GRANTED }
        statusView.append(if (denied.isEmpty()) "权限已授予 ✓\n" else "未授予: ${denied.joinToString()}\n")
    }

    // ---- UI 构建 ----

    private fun buildUi(): ScrollView {
        fun label(text: String): TextView = TextView(this).apply {
            this.text = text
            setPadding(0, dp(12), 0, dp(4))
        }

        nameInput = EditText(this).apply { setText(AppConfig.deviceName); textSize = 13f }

        // --- WS 组 ---
        urlInput = EditText(this).apply {
            setText(AppConfig.url); hint = AppConfig.URL_TEMPLATE; textSize = 13f
        }
        tokenInput = EditText(this).apply { setText(AppConfig.token); textSize = 13f }
        wsGroup = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            addView(label("网关地址（wss://host:port/device/ws）"))
            addView(urlInput)
            addView(label("配对 token（网关统一发放）"))
            addView(tokenInput)
        }

        // --- TCP 组 ---
        pairHostInput = EditText(this).apply {
            setText(AppConfig.pairingHost); hint = AppConfig.TCP_HOST_TEMPLATE; textSize = 13f
        }
        pairPortInput = EditText(this).apply {
            setText(AppConfig.pairingPort); hint = "18438"; textSize = 13f
        }
        pairCodeInput = EditText(this).apply {
            hint = "K7M2-P9XR"; textSize = 16f
            setSingleLine()
        }
        pairStatusView = TextView(this).apply {
            textSize = 12f
            setPadding(0, dp(6), 0, 0)
        }
        tcpGroup = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            addView(label("网关 TCP 地址（主机）"))
            addView(pairHostInput)
            addView(label("网关 TCP 端口"))
            addView(pairPortInput)
            addView(label("配对码（让 AI 生成，如 K7M2-P9XR）"))
            addView(pairCodeInput)
            addView(Button(this@MainActivity).apply {
                text = "配对"
                setOnClickListener { doPair() }
            })
            addView(pairStatusView)
        }

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

        val transportGroup = RadioGroup(this).apply { orientation = RadioGroup.HORIZONTAL }
        val wsRadio = RadioButton(this).apply { text = "WS (wss)"; id = View.generateViewId() }
        val tcpRadio = RadioButton(this).apply { text = "裸 TCP (Noise)"; id = View.generateViewId() }
        transportGroup.addView(wsRadio)
        transportGroup.addView(tcpRadio)
        if (AppConfig.transport == AppConfig.TRANSPORT_TCP) tcpRadio.isChecked = true else wsRadio.isChecked = true
        transportGroup.setOnCheckedChangeListener { _, checkedId ->
            val t = if (checkedId == tcpRadio.id) AppConfig.TRANSPORT_TCP else AppConfig.TRANSPORT_WS
            AppConfig.transport = t
            applyTransportVisibility(t)
            statusView.append("传输方式切换为 $t（需重新「保存并启动」）\n")
        }

        val buttonRow = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        fun addToRow(b: Button) {
            b.layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
                .apply { setMargins(dp(4), 0, dp(4), 0) }
            buttonRow.addView(b)
        }
        addToRow(Button(this).apply {
            text = "保存并启动"
            setOnClickListener { saveAndStart() }
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
        root.addView(label("传输方式"))
        root.addView(transportGroup)
        root.addView(wsGroup)
        root.addView(tcpGroup)
        root.addView(label("设备名称（登记身份，在线唯一）"))
        root.addView(nameInput)
        root.addView(buttonRow)
        root.addView(Button(this).apply {
            text = "授予日历/通知运行时权限"
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

    private fun applyTransportVisibility(transport: String) {
        val isTcp = transport == AppConfig.TRANSPORT_TCP
        wsGroup.visibility = if (isTcp) View.GONE else View.VISIBLE
        tcpGroup.visibility = if (isTcp) View.VISIBLE else View.GONE
    }

    // ---- 动作 ----

    private fun saveAndStart() {
        AppConfig.url = urlInput.text.toString().trim()
        AppConfig.token = tokenInput.text.toString().trim()
        AppConfig.pairingHost = pairHostInput.text.toString().trim()
        AppConfig.pairingPort = pairPortInput.text.toString().trim().ifBlank { "18438" }
        val name = nameInput.text.toString().trim()
        if (name.isBlank() || name.length > 32) {
            statusView.append("✗ 设备名必须是 1-32 个字符\n")
            return
        }
        AppConfig.deviceName = name

        val problem = AppConfig.validate()
        if (problem != null) {
            statusView.append("✗ $problem\n")
            return
        }
        // 重启服务以应用新配置
        stopService(Intent(this, GatewayService::class.java))
        startForegroundService(Intent(this, GatewayService::class.java))
        statusView.append("服务已启动（${AppConfig.transport}）\n")
    }

    private fun doPair() {
        val host = pairHostInput.text.toString().trim()
        val port = pairPortInput.text.toString().trim().toIntOrNull()
        val code = pairCodeInput.text.toString().trim()
        val name = nameInput.text.toString().trim()
        when {
            host.isBlank() -> { pairStatusView.text = "✗ 请填网关主机"; return }
            port == null || port !in 1..65535 -> { pairStatusView.text = "✗ 端口不合法"; return }
            code.isBlank() -> { pairStatusView.text = "✗ 请填配对码"; return }
            name.isBlank() -> { pairStatusView.text = "✗ 请填设备名"; return }
        }
        pairStatusView.text = "配对中…"
        io.submit {
            val result = runCatching {
                NoiseClient.pair(this, host, port, code, name)
            }
            runOnUiThread {
                result.onSuccess {
                    pairStatusView.text = "✓ 配对成功（网关公钥 ${it.take(12)}…）"
                    AppConfig.pairingHost = host
                    AppConfig.pairingPort = port.toString()
                    pairCodeInput.setText("")
                    refreshPairingStatus()
                }.onFailure {
                    pairStatusView.text = "✗ ${it.message ?: it.javaClass.simpleName}"
                }
            }
        }
    }

    private fun refreshPairingStatus() {
        val paired = PairingStore.load(this)
        pairStatusView.text = if (paired == null) {
            "未配对"
        } else {
            "已配对：${paired.host}:${paired.port}\n公钥 ${paired.gatewayPublicKeyB64.take(12)}…"
        }
    }

    private fun dp(v: Int): Int = (v * resources.displayMetrics.density).toInt()
}
