from bifrost.protocol import http_request, http_response


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
