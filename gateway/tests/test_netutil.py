"""X-Forwarded-For 还原真实客户端 IP（反代部署的安全关键点）。"""

from __future__ import annotations

from gateway.config import GatewayConfig, ListenConfig, McpConfig


def make_cfg(trusted: tuple[str, ...]) -> GatewayConfig:
    return GatewayConfig(
        listen=ListenConfig(host="127.0.0.1", port=0, trusted_proxies=trusted),
        mcp=McpConfig(path="/mcp", bearer="m" * 32),
        pairing_token="d" * 32,
    )


class _FakeWS:
    """只实现 client_ip 需要的两个属性。"""

    def __init__(self, peer: str, xff: str | None = None) -> None:
        class _Client:
            host = peer

        self.client = _Client()
        self.headers = {"x-forwarded-for": xff} if xff else {}


def test_no_trusted_proxies_ignores_xff():
    cfg = make_cfg(())
    assert cfg.listen.behind_proxy is False
    from gateway.netutil import client_ip

    assert client_ip(_FakeWS("203.0.113.9", "1.2.3.4"), cfg) == "203.0.113.9"


def test_untrusted_peer_cannot_spoof_xff():
    cfg = make_cfg(("127.0.0.1",))
    from gateway.netutil import client_ip

    # 攻击者直连且自带 XFF —— 必须被忽略
    assert client_ip(_FakeWS("203.0.113.9", "1.2.3.4"), cfg) == "203.0.113.9"


def test_trusted_proxy_uses_xff():
    cfg = make_cfg(("127.0.0.1", "::1"))
    from gateway.netutil import client_ip

    assert client_ip(_FakeWS("127.0.0.1", "198.51.100.7"), cfg) == "198.51.100.7"


def test_trusted_proxy_picks_rightmost_untrusted_hop():
    """多级反代：从右往左跳过受信代理，取第一个非受信项（防左侧伪造）。"""
    cfg = make_cfg(("127.0.0.1", "10.0.0.1"))
    from gateway.netutil import client_ip

    # 攻击者在最左侧塞了假 IP；真实值由最内层反代追加在右侧
    assert client_ip(_FakeWS("127.0.0.1", "1.2.3.4, 198.51.100.7, 10.0.0.1"), cfg) == "198.51.100.7"


def test_trusted_proxy_without_xff_falls_back_to_peer():
    cfg = make_cfg(("127.0.0.1",))
    from gateway.netutil import client_ip

    assert client_ip(_FakeWS("127.0.0.1"), cfg) == "127.0.0.1"


def test_cidr_trusted_proxy():
    cfg = make_cfg(("10.0.0.0/8",))
    from gateway.netutil import client_ip

    assert cfg.listen.is_trusted_proxy("10.5.6.7") is True
    assert cfg.listen.is_trusted_proxy("11.5.6.7") is False
    assert client_ip(_FakeWS("10.5.6.7", "198.51.100.7"), cfg) == "198.51.100.7"
