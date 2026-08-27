import asyncio

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from bifrost.client import ServiceProxy, configured_services
from bifrost.protocol import decode_body, encode_body


def config(services):
    complete_services = []
    for index, service in enumerate(services):
        complete = {
            'room': f'room-{index}',
            'local_port': 10080 + index,
            'ice_port': 37665 + index,
            'host': '127.0.0.1',
            'scheme': 'http',
            'password_hash': '',
        }
        complete.update(service)
        complete_services.append(complete)
    return {
        'signal': {'url': 'wss://example.test/signal', 'verify_tls': True},
        'webrtc': {'ice_servers': []},
        'auth': {
            'private_key': 'private',
            'timeout': 10,
        },
        'services': complete_services,
    }


async def proxy_request(proxy, message):
    replies = []

    async def reply(response):
        replies.append(response)

    await proxy(message, reply)
    assert len(replies) == 1
    return replies[0]


def test_empty_password_hash_means_public_room():
    base = config([
        {
            "room": "empty",
            "local_port": 8001,
            "host": "127.0.0.2",
            "scheme": "https",
            "password_hash": "",
        },
        {"room": "also-empty", "local_port": 8002},
    ])
    services = configured_services(base)

    assert services[0][2]["browser_auth"]["password_hash"] == ""
    assert services[1][2]["browser_auth"]["password_hash"] == ""
    assert services[0][1] == "https://127.0.0.2:8001"
    assert services[1][1] == "http://127.0.0.1:8002"


def test_service_rejects_plaintext_passwords():
    cfg = config([{"room": "home", "local_port": 8001}])
    cfg["services"][0]["password"] = "secret"
    with pytest.raises(ValueError, match="unsupported field: password"):
        configured_services(cfg)


def test_service_rejects_invalid_host_or_scheme():
    with pytest.raises(ValueError, match="invalid scheme"):
        configured_services(
            config([{"room": "home", "local_port": 8001, "scheme": "ftp"}])
        )
    with pytest.raises(ValueError, match="invalid host"):
        configured_services(
            config([{"room": "home", "local_port": 8001, "host": "http://x"}])
        )


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
def test_service_proxy_forwards_common_http_methods(method, body):
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
        try:
            async with aiohttp.ClientSession() as session:
                proxy = ServiceProxy(str(server.make_url("/")), session)
                response = await proxy_request(
                    proxy,
                    {
                        "type": "http_request",
                        "id": method,
                        "method": method,
                        "path": "/resource",
                        "headers": {"x-forwarded-test": "yes"},
                        "body_base64": encode_body(body),
                    },
                )
        finally:
            await server.close()

        assert response["status"] == 200
        assert response["headers"]["x-request-method"] == method
        assert decode_body(response["body_base64"]) == (
            body if method not in {"HEAD", "OPTIONS"} else b""
        )

    asyncio.run(scenario())


def test_service_proxy_reuses_cookies_and_reports_redirect_url():
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
        jar = aiohttp.CookieJar(unsafe=True)

        try:
            async with aiohttp.ClientSession(cookie_jar=jar) as session:
                proxy = ServiceProxy(str(server.make_url("/")), session)
                login = await proxy_request(
                    proxy,
                    {
                        "type": "http_request",
                        "id": "login",
                        "method": "POST",
                        "path": "/login",
                        "headers": {},
                        "body_base64": "",
                    },
                )
                account = await proxy_request(
                    proxy,
                    {
                        "type": "http_request",
                        "id": "account",
                        "method": "GET",
                        "path": "/account",
                        "headers": {},
                        "body_base64": "",
                    },
                )
        finally:
            await server.close()

        assert login["response_url"] == "/account"
        assert decode_body(login["body_base64"]) == b"active"
        assert decode_body(account["body_base64"]) == b"active"

    asyncio.run(scenario())


def test_service_proxy_relays_local_websocket_messages():
    async def scenario():
        async def websocket_handler(request):
            assert request.headers["Origin"] == str(server.make_url("/")).rstrip("/")
            websocket = web.WebSocketResponse(protocols=("bifrost-test",))
            await websocket.prepare(request)
            async for message in websocket:
                if message.type == aiohttp.WSMsgType.TEXT:
                    await websocket.send_str("echo:" + message.data)
                elif message.type == aiohttp.WSMsgType.BINARY:
                    await websocket.send_bytes(message.data[::-1])
            return websocket

        app = web.Application()
        app.router.add_get("/events", websocket_handler)
        server = TestServer(app)
        await server.start_server()
        replies = []
        received = asyncio.Event()

        async def reply(message):
            replies.append(message)
            if message["type"] in {"websocket_message", "websocket_closed"}:
                received.set()

        try:
            async with aiohttp.ClientSession() as session:
                proxy = ServiceProxy(str(server.make_url("/")), session)
                with pytest.raises(ValueError, match="websocket path"):
                    await proxy(
                        {
                            "type": "websocket_open",
                            "socket_id": "unsafe",
                            "path": "//other-host/events",
                        },
                        reply,
                    )
                with pytest.raises(ValueError, match="websocket protocols"):
                    await proxy(
                        {
                            "type": "websocket_open",
                            "socket_id": "bad-protocol",
                            "path": "/events",
                            "protocols": ["has spaces"],
                        },
                        reply,
                    )
                await proxy(
                    {
                        "type": "websocket_open",
                        "socket_id": "socket-1",
                        "path": "/events?after=2",
                        "protocols": ["bifrost-test"],
                    },
                    reply,
                )
                assert replies.pop(0) == {
                    "type": "websocket_opened",
                    "socket_id": "socket-1",
                    "protocol": "bifrost-test",
                }
                await proxy(
                    {
                        "type": "websocket_send",
                        "socket_id": "socket-1",
                        "data_type": "text",
                        "data": "hello",
                    },
                    reply,
                )
                await asyncio.wait_for(received.wait(), timeout=1)
                assert replies[0] == {
                    "type": "websocket_message",
                    "socket_id": "socket-1",
                    "data_type": "text",
                    "data": "echo:hello",
                }
                received.clear()
                await proxy(
                    {
                        "type": "websocket_send",
                        "socket_id": "socket-1",
                        "data_type": "binary",
                        "data": encode_body(b"abc"),
                    },
                    reply,
                )
                await asyncio.wait_for(received.wait(), timeout=1)
                assert replies[-1] == {
                    "type": "websocket_message",
                    "socket_id": "socket-1",
                    "data_type": "binary",
                    "data": encode_body(b"cba"),
                }
                received.clear()
                await proxy(
                    {
                        "type": "websocket_close",
                        "socket_id": "socket-1",
                        "code": 1000,
                        "reason": "done",
                    },
                    reply,
                )
                await asyncio.wait_for(received.wait(), timeout=1)
                assert replies[-1]["type"] == "websocket_closed"
                assert proxy.websockets == {}

                received.clear()
                await proxy(
                    {
                        "type": "websocket_open",
                        "socket_id": "socket-2",
                        "path": "/events",
                    },
                    reply,
                )
                assert replies.pop()["type"] == "websocket_opened"
                await proxy.close_connection(reply)
                assert proxy.websockets == {}
        finally:
            await server.close()

    asyncio.run(scenario())


def test_configured_services_supports_multiple_rooms():
    cfg = config([
        {'room': 'home', 'local_port': 10080},
        {'room': 'office', 'local_port': 10081, 'ice_port': 37666},
    ])
    cfg['webrtc'] = {'ice_servers': [{
        'urls': [
            'stun:stun.miwifi.com:3478',
            'stun:stun.cloudflare.com:3478',
        ],
    }]}
    services = configured_services(cfg)
    assert [(room, target) for room, target, _ in services] == [
        ('home', 'http://127.0.0.1:10080'),
        ('office', 'http://127.0.0.1:10081'),
    ]
    assert services[0][2]['signal']['room'] == 'home'
    assert services[1][2]['signal']['room'] == 'office'
    assert services[0][2]['webrtc']['ice_servers'] == cfg['webrtc']['ice_servers']


def test_empty_client_ice_servers_disable_client_side_stun():
    cfg = config([{'room': 'home', 'local_port': 10080}])

    services = configured_services(cfg)

    assert services[0][2]['webrtc']['ice_servers'] == []
    assert services[0][2]['webrtc']['ice_port'] == 37665


def test_missing_client_ice_config_is_rejected():
    cfg = config([{'room': 'home', 'local_port': 10080}])
    del cfg['webrtc']

    with pytest.raises(ValueError, match='missing required field: webrtc'):
        configured_services(cfg)


def test_missing_service_ice_port_is_rejected():
    cfg = config([{'room': 'home', 'local_port': 10080}])
    del cfg['services'][0]['ice_port']

    with pytest.raises(ValueError, match='missing required field: ice_port'):
        configured_services(cfg)


@pytest.mark.parametrize(
    "path",
    [
        ("signal",),
        ("webrtc",),
        ("services",),
        ("auth",),
        ("signal", "url"),
        ("signal", "verify_tls"),
        ("webrtc", "ice_servers"),
        ("auth", "private_key"),
        ("auth", "timeout"),
        ("services", 0, "room"),
        ("services", 0, "local_port"),
        ("services", 0, "ice_port"),
        ("services", 0, "host"),
        ("services", 0, "scheme"),
        ("services", 0, "password_hash"),
    ],
)
def test_client_rejects_every_missing_required_field(path):
    cfg = config([{'room': 'home', 'local_port': 10080}])
    target = cfg
    for key in path[:-1]:
        target = target[key]
    del target[path[-1]]

    with pytest.raises(ValueError, match=f"missing required field: {path[-1]}"):
        configured_services(cfg)


def test_client_rejects_unknown_config_fields():
    cfg = config([{'room': 'home', 'local_port': 10080}])
    cfg['signal']['retry_delay'] = 5

    with pytest.raises(ValueError, match='unsupported field: retry_delay'):
        configured_services(cfg)


def test_client_accepts_turn_ice_servers():
    cfg = config([{'room': 'home', 'local_port': 10080}])
    cfg['webrtc'] = {'ice_servers': [{
        'urls': ['turn:turn.example.com:3478?transport=udp'],
        'username': 'bifrost',
        'credential': 'secret',
    }]}

    services = configured_services(cfg)

    assert services[0][2]['webrtc'] == {
        **cfg['webrtc'],
        'ice_port': 37665,
    }


def test_client_passes_a_distinct_fixed_ice_port_to_each_room():
    cfg = config([
        {'room': 'home', 'local_port': 10080, 'ice_port': 40000},
        {'room': 'office', 'local_port': 10081, 'ice_port': 40001},
    ])

    services = configured_services(cfg)

    assert services[0][2]['webrtc']['ice_port'] == 40000
    assert services[1][2]['webrtc']['ice_port'] == 40001


@pytest.mark.parametrize('ice_port', [0, 65536, True, 40000.5, '40000'])
def test_client_rejects_invalid_fixed_ice_port(ice_port):
    cfg = config([{
        'room': 'home',
        'local_port': 10080,
        'ice_port': ice_port,
    }])

    with pytest.raises((TypeError, ValueError), match='ice_port'):
        configured_services(cfg)


def test_client_rejects_duplicate_fixed_ice_ports():
    cfg = config([
        {'room': 'home', 'local_port': 10080, 'ice_port': 40000},
        {'room': 'office', 'local_port': 10081, 'ice_port': 40000},
    ])

    with pytest.raises(ValueError, match='duplicate ice_port'):
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
