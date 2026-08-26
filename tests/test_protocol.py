import pytest

from bifrost.protocol import (
    decode_body,
    default_config_path,
    encode_body,
    http_request,
    http_response,
    resolve_config_path,
    validate_ice_servers,
    validate_stun_urls,
    webrtc_ice_servers,
)


def test_default_config_path_requires_component_toml(tmp_path):
    with pytest.raises(FileNotFoundError, match="server config not found"):
        default_config_path("server", tmp_path)

    config_path = tmp_path / "server.toml"
    config_path.write_text("[server]\n", encoding="utf-8")
    assert default_config_path("server", tmp_path) == config_path
    assert resolve_config_path(None, "server", tmp_path) == config_path
    explicit_path = tmp_path / "custom.toml"
    assert resolve_config_path(str(explicit_path), "server") == explicit_path


def test_default_config_path_rejects_unknown_component(tmp_path):
    with pytest.raises(ValueError, match="unsupported config component"):
        default_config_path("worker", tmp_path)


def test_validate_stun_urls_accepts_udp_stun_hosts_and_empty_list():
    urls = ["stun:stun.example.com:3478", "stun:[2001:db8::1]:3478"]

    assert validate_stun_urls(urls) == urls
    assert validate_stun_urls([]) == []


@pytest.mark.parametrize(
    "value",
    [
        "stun:stun.example.com:3478",
        ["https://stun.example.com"],
        ["stun:stun.example.com:70000"],
        ["stun:user@stun.example.com"],
        ["stun:stun.example.com", "stun:stun.example.com"],
        [1],
    ],
)
def test_validate_stun_urls_rejects_invalid_values(value):
    with pytest.raises((TypeError, ValueError), match="stun"):
        validate_stun_urls(value)


def test_validate_ice_servers_accepts_stun_and_authenticated_turn():
    servers = [
        {"urls": "stun:stun.example.com:3478"},
        {
            "urls": [
                "turn:turn.example.com:3478?transport=udp",
                "turns:turn.example.com:5349?transport=tcp",
            ],
            "username": "bifrost",
            "credential": "secret",
        },
    ]

    assert validate_ice_servers(servers) == [
        {"urls": ["stun:stun.example.com:3478"]},
        {
            "urls": [
                "turn:turn.example.com:3478?transport=udp",
                "turns:turn.example.com:5349?transport=tcp",
            ],
            "username": "bifrost",
            "credential": "secret",
        },
    ]


@pytest.mark.parametrize(
    "servers",
    [
        [{"urls": ["https://turn.example.com"]}],
        [{"urls": ["turn:turn.example.com:3478"]}],
        [{"urls": ["turns:turn.example.com:5349?transport=udp"], "username": "u", "credential": "p"}],
        [{"urls": ["stun:stun.example.com"], "username": "u", "credential": "p"}],
        [{"urls": ["stun:stun.example.com"], "password": "typo"}],
    ],
)
def test_validate_ice_servers_rejects_unusable_entries(servers):
    with pytest.raises(
        (TypeError, ValueError), match="stun|ICE|TURN|credential|unsupported"
    ):
        validate_ice_servers(servers)


def test_webrtc_ice_servers_keeps_legacy_stun_compatibility():
    urls = ["stun:stun.example.com:3478"]

    assert webrtc_ice_servers({"stun_urls": urls}) == [{"urls": urls}]
    with pytest.raises(ValueError, match="both ice_servers and stun_urls"):
        webrtc_ice_servers({"stun_urls": [], "ice_servers": []})


def test_http_request_defaults():
    assert http_request("1", "GET", "") == {
        "type": "http_request",
        "id": "1",
        "method": "GET",
        "path": "/",
        "headers": {},
        "body": "",
    }
def test_http_response_with_error():
    response = http_response(2, 502, error="upstream unavailable")
    assert response["type"] == "http_response"
    assert response["id"] == 2
    assert response["status"] == 502
    assert response["error"] == "upstream unavailable"


def test_http_request_preserves_non_get_method_and_binary_body():
    encoded = encode_body(b"\x00payload\xff")
    assert http_request("1", "PATCH", "/resource", body_base64=encoded) == {
        "type": "http_request",
        "id": "1",
        "method": "PATCH",
        "path": "/resource",
        "headers": {},
        "body": "",
        "body_base64": encoded,
    }
    assert decode_body(encoded) == b"\x00payload\xff"


def test_http_response_can_carry_binary_body_and_status_text():
    encoded = encode_body(b"\x89PNG\r\n")
    response = http_response(
        "1",
        201,
        {"content-type": "image/png"},
        body_base64=encoded,
        status_text="Created",
        response_url="/images/logo.png",
    )
    assert response["body_base64"] == encoded
    assert response["status_text"] == "Created"
    assert response["response_url"] == "/images/logo.png"
