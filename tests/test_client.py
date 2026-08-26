import asyncio
import json

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from bifrost.client import configured_services, handle_http
from bifrost.protocol import decode_body, encode_body


class Channel:
    def __init__(self):
        self.messages = []

    def send(self, raw):
        self.messages.append(json.loads(raw))


def test_empty_or_missing_password_hash_means_public_room():
    base = {
        "signal": {"url": "wss://example.test/signal"},
        "auth": {},
        "services": [
            {
                "room": "empty",
                "local_port": 8001,
                "host": "127.0.0.2",
                "scheme": "https",
                "password_hash": "",
            },
            {"room": "missing", "local_port": 8002},
        ],
    }
    services = configured_services(base)

    assert services[0][2]["browser_auth"]["password_hash"] == ""
    assert services[1][2]["browser_auth"]["password_hash"] == ""
    assert services[0][1] == "https://127.0.0.2:8001"
    assert services[1][1] == "http://127.0.0.1:8002"


def test_service_rejects_plaintext_passwords():
    with pytest.raises(ValueError, match="unsupported password"):
        configured_services(
            {
                "signal": {},
                "auth": {},
                "services": [
                    {
                        "room": "home",
                        "local_port": 8001,
                        "password": "secret",
                    }
                ],
            }
        )


def test_service_rejects_invalid_host_or_scheme():
    with pytest.raises(ValueError, match="invalid scheme"):
        configured_services(
            config([{"room": "home", "local_port": 8001, "scheme": "ftp"}])
        )
    with pytest.raises(ValueError, match="invalid host"):
        configured_services(
            config([{"room": "home", "local_port": 8001, "host": "http://x"}])
        )


def test_legacy_local_http_is_a_service_default():
    cfg = config([{"room": "home", "local_port": 8001}])
    cfg["local_http"] = {"host": "legacy.test", "scheme": "https"}

    assert configured_services(cfg)[0][1] == "https://legacy.test:8001"


@pytest.mark.parametrize(
    ("method", "body"),
    [
        ("GET", b""),
        ("POST", b"post body"),
        ("PUT", b"put body"),
        ("PATCH", b"patch body"),
        ("DELETE", b"delete body"),
        ("HEAD", b""),
        ("OPTIONS", b""),
    ],
)
def test_handle_http_forwards_common_methods(method, body):
    async def scenario():
        async def resource(request):
            received = await request.read()
            return web.Response(
                body=received if request.method != "HEAD" else None,
                headers={"x-request-method": request.method},
            )

        app = web.Application()
        app.router.add_route("*", "/resource", resource)
        server = TestServer(app)
        await server.start_server()
        channel = Channel()

        try:
            async with aiohttp.ClientSession() as session:
                await handle_http(
                    channel,
                    json.dumps(
                        {
                            "type": "http_request",
                            "id": method,
                            "method": method,
                            "path": "/resource",
                            "headers": {"x-forwarded-test": "yes"},
                            "body_base64": encode_body(body),
                        }
                    ),
                    str(server.make_url("/")),
                    session,
                )
        finally:
            await server.close()

        response = channel.messages.pop()
        assert response["status"] == 200
        assert response["headers"]["x-request-method"] == method
        assert decode_body(response["body_base64"]) == (
            body if method not in {"HEAD", "OPTIONS"} else b""
        )

    asyncio.run(scenario())


def test_handle_http_reuses_cookies_and_reports_redirect_url():
    async def scenario():
        async def login(request):
            response = web.HTTPFound("/account")
            response.set_cookie("session", "active")
            raise response

        async def account(request):
            return web.Response(text=request.cookies.get("session", "missing"))

        app = web.Application()
        app.router.add_post("/login", login)
        app.router.add_get("/account", account)
        server = TestServer(app)
        await server.start_server()
        channel = Channel()
        jar = aiohttp.CookieJar(unsafe=True)

        try:
            async with aiohttp.ClientSession(cookie_jar=jar) as session:
                await handle_http(
                    channel,
                    json.dumps(
                        {
                            "type": "http_request",
                            "id": "login",
                            "method": "POST",
                            "path": "/login",
                        }
                    ),
                    str(server.make_url("/")),
                    session,
                )
                await handle_http(
                    channel,
                    json.dumps(
                        {
                            "type": "http_request",
                            "id": "account",
                            "method": "GET",
                            "path": "/account",
                        }
                    ),
                    str(server.make_url("/")),
                    session,
                )
        finally:
            await server.close()

        login, account = channel.messages
        assert login["response_url"] == "/account"
        assert decode_body(login["body_base64"]) == b"active"
        assert decode_body(account["body_base64"]) == b"active"

    asyncio.run(scenario())


def config(services):
    return {
        'signal': {'url': 'wss://example.test/signal', 'verify_tls': True},
        'auth': {'private_key': 'private', 'public_key': 'public'},
        'services': services,
    }


def test_configured_services_supports_multiple_rooms():
    cfg = config([
        {'room': 'home', 'local_port': 10080},
        {'room': 'office', 'local_port': 10081},
    ])
    cfg['webrtc'] = {
        'stun_urls': [
            'stun:stun.miwifi.com:3478',
            'stun:stun.cloudflare.com:3478',
        ]
    }
    services = configured_services(cfg)
    assert [(room, target) for room, target, _ in services] == [
        ('home', 'http://127.0.0.1:10080'),
        ('office', 'http://127.0.0.1:10081'),
    ]
    assert services[0][2]['signal']['room'] == 'home'
    assert services[1][2]['signal']['room'] == 'office'
    assert services[0][2]['webrtc']['stun_urls'] == cfg['webrtc']['stun_urls']


def test_empty_client_stun_urls_are_preserved_for_server_fallback():
    cfg = config([{'room': 'home', 'local_port': 10080}])
    cfg['webrtc'] = {'stun_urls': []}

    services = configured_services(cfg)

    assert services[0][2]['webrtc']['stun_urls'] == []


def test_client_accepts_turn_ice_servers():
    cfg = config([{'room': 'home', 'local_port': 10080}])
    cfg['webrtc'] = {'ice_servers': [{
        'urls': ['turn:turn.example.com:3478?transport=udp'],
        'username': 'bifrost',
        'credential': 'secret',
    }]}

    services = configured_services(cfg)

    assert services[0][2]['webrtc'] == cfg['webrtc']


@pytest.mark.parametrize(
    'stun_urls',
    ['stun:example.com:3478', ['turn:example.com:3478'], [42]],
)
def test_client_rejects_invalid_stun_configuration(stun_urls):
    cfg = config([{'room': 'home', 'local_port': 10080}])
    cfg['webrtc'] = {'stun_urls': stun_urls}

    with pytest.raises((TypeError, ValueError), match='stun'):
        configured_services(cfg)


def test_configured_services_rejects_duplicate_rooms():
    with pytest.raises(ValueError, match='duplicate room'):
        configured_services(config([
            {'room': 'home', 'local_port': 10080},
            {'room': 'home', 'local_port': 10081},
        ]))


def test_configured_services_rejects_invalid_port():
    with pytest.raises(ValueError, match='invalid local_port'):
        configured_services(config([{'room': 'home', 'local_port': 70000}]))
