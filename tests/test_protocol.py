from bifrost.protocol import http_response


def test_http_response_with_error():
    response = http_response(2, 502, error="upstream unavailable")
    assert response["type"] == "http_response"
    assert response["id"] == 2
    assert response["status"] == 502
    assert response["error"] == "upstream unavailable"
