package dev.amg.device

import android.content.Context
import org.json.JSONObject

/** 设备端工具执行错误，code 对应协议 §4 错误码表。 */
class ToolError(val code: String, message: String) : Exception(message)

/** 工具执行器签名。 */
typealias ToolExecutor = (Context, JSONObject) -> JSONObject

/**
 * 工具注册表。名称集合与网关 catalog.py 一一对应；
 * args 校验在设备端做（协议 §6：单一权威）——这里做必要字段/类型检查。
 */
object ToolRegistry {
    private val executors = LinkedHashMap<String, ToolExecutor>()

    fun register(name: String, executor: ToolExecutor) {
        executors[name] = executor
    }

    fun execute(context: Context, name: String, args: JSONObject): JSONObject {
        val executor = executors[name] ?: throw ToolError("TOOL_NOT_FOUND", "未知工具: $name")
        return executor(context, args)
    }

    // ---- 入参校验助手（ARGS_INVALID）----

    fun requireString(args: JSONObject, key: String): String {
        if (!args.has(key) || args.isNull(key)) throw ToolError("ARGS_INVALID", "缺少必填字段: $key")
        val v = args.opt(key)
        if (v !is String || v.isBlank()) throw ToolError("ARGS_INVALID", "字段 $key 必须是非空字符串")
        return v
    }

    fun requireLong(args: JSONObject, key: String): Long {
        if (!args.has(key) || args.isNull(key)) throw ToolError("ARGS_INVALID", "缺少必填字段: $key")
        val v = args.opt(key)
        val n = (v as? Number)?.toLong() ?: (v as? String)?.toLongOrNull()
        return n ?: throw ToolError("ARGS_INVALID", "字段 $key 必须是整数")
    }

    fun optString(args: JSONObject, key: String): String? =
        if (args.has(key) && !args.isNull(key)) args.optString(key) else null

    fun optLong(args: JSONObject, key: String): Long? =
        if (args.has(key) && !args.isNull(key)) {
            (args.opt(key) as? Number)?.toLong() ?: throw ToolError("ARGS_INVALID", "字段 $key 必须是整数")
        } else null

    fun optBool(args: JSONObject, key: String, default: Boolean): Boolean =
        if (args.has(key) && !args.isNull(key)) args.optBoolean(key) else default
}
