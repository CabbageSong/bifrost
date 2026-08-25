from bifrost.agent import rtc_configuration, select_stun_urls


def test_client_stun_urls_take_precedence_over_server_urls():
    configured = ["stun:client.example:3478"]
    server = ["stun:server.example:3478"]

    assert select_stun_urls(configured, server) == configured
    assert select_stun_urls([], server) == server


def test_rtc_configuration_never_uses_implicit_aiortc_defaults():
    empty = rtc_configuration([])
    configured = rtc_configuration([
        "stun:stun.miwifi.com:3478",
        "stun:stun.cloudflare.com:3478",
    ])

    assert empty.iceServers == []
    assert len(configured.iceServers) == 1
    assert configured.iceServers[0].urls == [
        "stun:stun.miwifi.com:3478",
        "stun:stun.cloudflare.com:3478",
    ]
