import pytest

from bifrost.protocol import (
    CHUNK_SIZE,
    decode_body,
    decode_packet,
    default_config_path,
    encode_body,
    encode_packet,
    http_chunk,
    http_response,
    resolve_config_path,
    validate_ice_servers,
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


def test_validate_ice_servers_accepts_stun_and_authenticated_turn():
    servers = [
        {
            "urls": "stun:stun.example.com:3478",
        },
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
        {
            "urls": ["stun:stun.example.com:3478"],
        },
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
        (TypeError, ValueError), match="stun|ICE|TURN|credential|unsupported|required"
    ):
        validate_ice_servers(servers)


def test_http_response_with_error():
    response = http_response(2, 502, {}, error="upstream unavailable")
    assert response["type"] == "http_response"
    assert response["id"] == 2
    assert response["status"] == 502
    assert response["error"] == "upstream unavailable"


def test_body_encoding_preserves_binary_data():
    encoded = encode_body(b"\x00payload\xff")
    assert decode_body(encoded) == b"\x00payload\xff"


def test_http_response_can_carry_binary_body_and_status_text():
    response = http_response(
        "1",
        201,
        {"content-type": "image/png"},
        body=b"\x89PNG\r\n",
        status_text="Created",
        response_url="/images/logo.png",
    )
    assert response["body"] == b"\x89PNG\r\n"
    assert response["status_text"] == "Created"
    assert response["response_url"] == "/images/logo.png"


def test_packet_encoding_and_decoding():
    header = {"type": "http_request", "id": 1, "path": "/test"}
    body = b"\x00\x01\x02\xffbinary\xfe"
    packet = encode_packet(header, body)
    decoded_header, decoded_body = decode_packet(packet)
    assert decoded_header == header
    assert decoded_body == body


def test_packet_encoding_with_body_in_header():
    header = {"type": "http_response", "id": 2, "body": b"payload"}
    packet = encode_packet(header)
    decoded_header, decoded_body = decode_packet(packet)
    assert "body" not in decoded_header
    assert decoded_header == {"type": "http_response", "id": 2}
    assert decoded_body == b"payload"


def test_packet_decoding_errors():
    with pytest.raises(ValueError, match="packet too short"):
        decode_packet(b"\x00\x00")
    with pytest.raises(ValueError, match="packet header truncated"):
        decode_packet(b"\x00\x00\x00\x10abc")


def test_packet_encoding_strips_body_base64_and_handles_none_body():
    header = {
        "type": "http_response",
        "id": 3,
        "body": None,
        "body_base64": "eyJpZCI6MX0=",
    }
    packet = encode_packet(header)
    decoded_header, decoded_body = decode_packet(packet)
    assert "body" not in decoded_header
    assert "body_base64" not in decoded_header
    assert decoded_header == {"type": "http_response", "id": 3}
    assert decoded_body == b""


def test_chunked_http_response_and_chunk_helpers():
    resp = http_response(10, 200, {"content-type": "text/plain"}, chunked=True, total_size=100000)
    assert resp["type"] == "http_response"
    assert resp["chunked"] is True
    assert resp["total_size"] == 100000
    assert resp["body"] == b""

    chunk = http_chunk(10, 0, eof=False, body=b"hello")
    assert chunk["type"] == "http_chunk"
    assert chunk["id"] == 10
    assert chunk["seq"] == 0
    assert chunk["eof"] is False
    assert chunk["body"] == b"hello"

    last_chunk = http_chunk(10, 1, eof=True, body=b"world")
    assert last_chunk["eof"] is True
    assert last_chunk["body"] == b"world"

    err_chunk = http_chunk(10, 2, error="connection reset")
    assert err_chunk["error"] == "connection reset"
