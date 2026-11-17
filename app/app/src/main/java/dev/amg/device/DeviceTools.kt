package dev.amg.device

import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.BatteryManager
import android.os.Build
import org.json.JSONObject

/** device_status：让 AI 感知设备状态（电量/充电/在线）。 */
object DeviceTools {

    fun register() {
        ToolRegistry.register("device_status", ::status)
    }

    private fun status(ctx: Context, args: JSONObject): JSONObject {
        val intent = ctx.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        val level = intent?.getIntExtra(BatteryManager.EXTRA_LEVEL, -1) ?: -1
        val scale = intent?.getIntExtra(BatteryManager.EXTRA_SCALE, -1) ?: -1
        val batteryPct = if (level >= 0 && scale > 0) (level * 100 / scale) else null
        val plugged = intent?.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) ?: 0
        val chargeStatus = intent?.getIntExtra(BatteryManager.EXTRA_STATUS, -1) ?: -1
        val charging = plugged != 0 ||
            chargeStatus == BatteryManager.BATTERY_STATUS_CHARGING ||
            chargeStatus == BatteryManager.BATTERY_STATUS_FULL

        val out = JSONObject()
            .put("model", Build.MODEL)
            .put("android", Build.VERSION.SDK_INT)
            .put("app_version", AppConfig.APP_VERSION)
            .put("charging", charging)
            .put("online", true) // 能执行本工具即在线；离线时网关直接报 DEVICE_OFFLINE，到不了这里
        if (batteryPct != null) out.put("battery_level", batteryPct)
        return out
    }
}
