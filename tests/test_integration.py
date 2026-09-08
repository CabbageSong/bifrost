"""End-to-end tests for the server, client and HTTP forwarding path."""

import asyncio
import socket

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bifrost import client
from bifrost.auth import public_key_text
from bifrost.curl import BifrostHTTPClient
from bifrost.room_auth import hash_password
from bifrost.server import create_app, rooms


def _free_udp_port():
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]
    finally:
        probe.close()


async def _wait_for_room(room):
    for _ in range(200):
        state = rooms.get(room)
        if state and state.get("agent") and not state["agent"].closed:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"client did not register room {room!r}")


def test_server_client_http_methods_end_to_end(tmp_path):
    async def scenario():
        rooms.clear()
        private_key = Ed25519PrivateKey.generate()
        private_key_path = tmp_path / "agent_ed25519"
        private_key_path.write_bytes(
            private_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        password_hash = hash_password("room-secret", salt=b"0123456789abcdef")

        async def resource(request):
            body = await request.read()
            return web.Response(
                body=body,
                headers={
                    "x-request-method": request.method,
                    "x-request-path": request.raw_path,
                    "x-forwarded-test": request.headers.get(
                        "x-forwarded-test", "missing"
                    ),
                },
            )

        local_app = web.Application()
        local_app.router.add_route("*", "/resource", resource)
        local_server = TestServer(local_app)

        server_cfg = {
            "server": {"bind": "127.0.0.1", "port": 1},
            "webrtc": {"ice_servers": []},
            "tls": {"cert": "", "key": ""},
            "auth": {
                "public_keys": [public_key_text(private_key.public_key())],
                "timeout": 2,
            },
            "browser_auth": {
                "session_ttl": 300,
                "max_attempts": 5,
                "attempt_window": 60,
                "password_workers": 1,
            },
        }

        signal_client = None
        client_task = None
        try:
            await local_server.start_server()
            local_url = local_server.make_url("/resource")
            signal_client = TestClient(TestServer(create_app(server_cfg)))
            await signal_client.start_server()

            client_cfg = {
                "signal": {
                    "url": str(signal_client.make_url("/signal")),
                    "verify_tls": True,
                },
                "webrtc": {"ice_servers": []},
                "auth": {"private_key": str(private_key_path), "timeout": 2},
                "services": [
                    {
                        "room": "home",
                        "local_port": local_url.port,
                        "ice_port": _free_udp_port(),
                        "host": local_url.host,
                        "scheme": local_url.scheme,
                        "password_hash": password_hash,
                    }
                ],
            }
            client_task = asyncio.create_task(client.run(client_cfg))
            await _wait_for_room("home")

            cases = [
                ("GET", b""),
                ("POST", b"post body"),
                ("PUT", b"put body"),
                ("PATCH", b"patch body"),
                ("DELETE", b"delete body"),
                ("HEAD", b""),
                ("OPTIONS", b"options body"),
            ]
            async with BifrostHTTPClient(
                str(signal_client.make_url("/")),
                "home",
                password="room-secret",
                timeout=10,
            ) as browser:
                for method, body in cases:
                    response = await browser.request(
                        method,
                        "/resource?from=e2e",
                        headers={"x-forwarded-test": "yes"},
                        body=body,
                    )
                    assert response.status == 200
                    assert response.headers["x-request-method"] == method
                    assert response.headers["x-forwarded-test"] == "yes"
                    expected_body = b"" if method == "HEAD" else body
                    assert response.body == expected_body
                    assert response.headers["x-request-path"] == "/resource?from=e2e"
        finally:
            if client_task is not None:
                client_task.cancel()
                await asyncio.gather(client_task, return_exceptions=True)
            if signal_client is not None:
                await signal_client.close()
            await local_server.close()
            rooms.clear()

    asyncio.run(scenario())
