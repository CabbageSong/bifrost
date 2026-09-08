import asyncio
from io import BytesIO

from bifrost import curl


def test_parse_page_config_supports_nested_ice_entries():
    html = (
        '<script>const bifrostConfig = '
        '{"room":"home","iceServers":['
        '{"urls":["turn:turn.example:3478?transport=tcp"],'
        '"username":"user","credential":"secret"}]};</script>'
    )

    assert curl._parse_page_config(html) == [
        {
            "urls": ["turn:turn.example:3478?transport=tcp"],
            "username": "user",
            "credential": "secret",
        }
    ]


def test_curl_helpers_parse_headers_and_data(tmp_path):
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"\x00\xff")

    assert curl._parse_headers(["X-Test: yes", "Content-Type: application/octet-stream"]) == {
        "X-Test": "yes",
        "Content-Type": "application/octet-stream",
    }
    assert curl._read_data(["first", "second"], BytesIO()) == (
        b"first&second"
    )
    assert curl._read_data([f"@{payload}"], BytesIO()) == b"\x00\xff"


def test_curl_run_uses_post_for_data_and_writes_binary_response(monkeypatch, tmp_path):
    calls = []

    class FakeConnection:
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))

        async def __aenter__(self):
            return self

        async def __aexit__(self, _type, _value, _traceback):
            pass

        async def request(self, method, path, *, headers, body):
            calls.append((method, path, headers, body))
            return curl.HTTPResponse(201, "Created", {"x-test": "yes"}, b"\x00\xff")

    monkeypatch.setattr(curl, "BifrostHTTPClient", FakeConnection)
    output = tmp_path / "response.bin"
    args = curl._parser().parse_args(
        [
            "--server",
            "http://127.0.0.1:8443",
            "--room",
            "home",
            "-H",
            "X-Request: yes",
            "-d",
            "a=1",
            "-d",
            "b=2",
            "-o",
            str(output),
            "/resource",
        ]
    )

    assert asyncio.run(curl._run(args)) == 0
    assert calls[1] == (
        "POST",
        "/resource",
        {
            "X-Request": "yes",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        b"a=1&b=2",
    )
    assert output.read_bytes() == b"\x00\xff"


def test_curl_run_rejects_two_password_sources():
    args = curl._parser().parse_args(
        [
            "--server",
            "http://127.0.0.1:8443",
            "--room",
            "home",
            "--password",
            "one",
            "--password-stdin",
            "/resource",
        ]
    )

    try:
        asyncio.run(curl._run(args))
    except ValueError as exc:
        assert str(exc) == "--password and --password-stdin cannot be used together"
    else:
        raise AssertionError("expected conflicting password options to fail")
