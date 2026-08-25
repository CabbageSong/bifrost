from bifrost.protocol import decode_body, encode_body, http_request, http_response


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
    )
    assert response["body_base64"] == encoded
    assert response["status_text"] == "Created"
