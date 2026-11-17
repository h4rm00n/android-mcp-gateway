"""网关内部错误类型。"""


class DeviceGone(Exception):
    """设备不可达（离线/被顶替/断连）。在途调用立即失败，不傻等超时（协议 §5）。"""

    def __init__(self, code: str = "DEVICE_OFFLINE", message: str = "设备离线") -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class DeviceCallTimeout(Exception):
    """网关侧按 timeout_ms 计时超时（协议 §7）。"""

    def __init__(self, call_id: str, tool: str) -> None:
        super().__init__(f"tool {tool} 超时")
        self.call_id = call_id
