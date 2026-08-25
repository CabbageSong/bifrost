import pytest

from bifrost.protocol import (
    decode_body,
    default_config_path,
    encode_body,
    http_request,
    http_response,
    resolve_config_path,
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
