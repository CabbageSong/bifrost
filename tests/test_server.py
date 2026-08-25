import asyncio
import base64
import re
from http.cookies import SimpleCookie

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bifrost.auth import auth_payload, public_key_text
from bifrost.room_auth import hash_password
from bifrost.server import LoginLimiter, create_app, create_ssl_context, rooms


def test_empty_tls_paths_select_http_and_partial_paths_fail():
    assert create_ssl_context({"cert": "", "key": ""}) is None
    assert create_ssl_context({}) is None
    with pytest.raises(ValueError, match="both be empty or both be set"):
        create_ssl_context({"cert": "/tmp/cert.pem", "key": ""})
    with pytest.raises(TypeError, match="must be strings"):
        create_ssl_context({"cert": None, "key": None})


async def authenticate_agent(client, private_key, room, password_hash=""):
    agent = await client.ws_connect(f"/signal?role=agent&room={room}")
    challenge_message = await agent.receive_json()
    challenge = base64.b64decode(challenge_message["challenge"])
    signature = private_key.sign(auth_payload(room, challenge, password_hash))
    await agent.send_json(
        {
            "type": "auth_response",
            "public_key": public_key_text(private_key.public_key()),
            "signature": base64.b64encode(signature).decode("ascii"),
            "password_hash": password_hash,
        }
    )
    assert await agent.receive_json() == {"type": "auth_ok"}
    return agent


def test_login_limiter_expires_failed_attempts():
    limiter = LoginLimiter(attempts=2, window=60)
    key = ("home", "127.0.0.1")

    limiter.failed(key, now=100)
    assert limiter.retry_after(key, now=101) == 0
    limiter.failed(key, now=102)
    assert limiter.retry_after(key, now=103) == 57
    assert limiter.retry_after(key, now=161) == 0


def test_agent_registers_room_before_browser_can_join():
    async def scenario():
        rooms.clear()
        private_key = Ed25519PrivateKey.generate()
        app = create_app(
            {
                "auth": {
                    "public_keys": [public_key_text(private_key.public_key())],
                    "timeout": 1,
                }
            }
        )
        client = TestClient(TestServer(app))
        await client.start_server()

        try:
            missing_room = await client.get("/signal?role=client")
            assert missing_room.status == 400
            assert await missing_room.text() == "missing room"

            unregistered_room = await client.get("/signal?role=client&room=home")
            assert unregistered_room.status == 409
            assert await unregistered_room.text() == "room not ready"

            agent = await authenticate_agent(client, private_key, "home")
            browser = await client.ws_connect("/signal?role=client&room=home")

            assert await browser.receive_json() == {"type": "agent_online"}
            await browser.send_json({"type": "offer", "sdp": {"type": "offer"}})
            assert await agent.receive_json() == {
                "type": "offer",
                "sdp": {"type": "offer"},
            }

            await browser.close()
            await agent.close()
        finally:
            await client.close()

        assert rooms == {}

    asyncio.run(scenario())


def test_room_portal_and_protected_room_login():
    async def scenario():
        rooms.clear()
        private_key = Ed25519PrivateKey.generate()
        password_hash = hash_password("open sesame", salt=b"t" * 16)
        app = create_app(
            {
                "auth": {
                    "public_keys": [public_key_text(private_key.public_key())],
                    "timeout": 1,
                },
                "browser_auth": {"session_ttl": 300},
            }
        )
        async with TestClient(TestServer(app)) as client:
            portal = await client.get("/")
            assert portal.status == 200
            assert "进入 Bifrost Room" in await portal.text()

            redirect = await client.get("/?room=home", allow_redirects=False)
            assert redirect.status == 302
            assert redirect.headers["Location"] == "/home"

            agent = await authenticate_agent(
                client, private_key, "home", password_hash
            )
            protected = await client.get("/home/admin?tab=users")
            assert protected.status == 401
            protected_text = await protected.text()
            assert "需要访问密码" in protected_text
            assert 'value="/home/admin?tab=users"' in protected_text
            login_token = re.search(
                r'name="login_token" value="([^"]+)"', protected_text
            ).group(1)

            with pytest.raises(aiohttp.WSServerHandshakeError) as rejected:
                await client.ws_connect("/signal?role=client&room=home")
            assert rejected.value.status == 401

            wrong = await client.post(
                "/_bifrost/login",
                data={
                    "room": "home",
                    "password": "wrong",
                    "next": "/home/admin?tab=users",
                    "login_token": login_token,
                },
            )
            assert wrong.status == 401
            wrong_text = await wrong.text()
            assert "密码不正确" in wrong_text
            retry_token = re.search(
                r'name="login_token" value="([^"]+)"', wrong_text
            ).group(1)

            success = await client.post(
                "/_bifrost/login",
                data={
                    "room": "home",
                    "password": "open sesame",
                    "next": "/home/admin?tab=users",
                    "login_token": retry_token,
                },
                allow_redirects=False,
            )
            assert success.status == 302
            assert success.headers["Location"] == "/home/admin?tab=users"
            set_cookie = success.headers["Set-Cookie"]
            assert "Secure" not in set_cookie
            assert "HttpOnly" in set_cookie
            assert "SameSite=Lax" in set_cookie
            parsed_cookie = SimpleCookie()
            parsed_cookie.load(set_cookie)
            name, morsel = next(iter(parsed_cookie.items()))
            auth_headers = {"Cookie": f"{name}={morsel.value}"}

            room_page = await client.get(
                "/home/admin?tab=users", headers=auth_headers
            )
            assert room_page.status == 200
            assert room_page.headers["X-Frame-Options"] == "DENY"
            text = await room_page.text()
            assert '"room":"home"' in text
            assert '"path":"/admin?tab=users"' in text

            with pytest.raises(aiohttp.WSServerHandshakeError) as bad_origin:
                await client.ws_connect(
                    "/signal?role=client&room=home",
                    headers={**auth_headers, "Origin": "https://attacker.example"},
                )
            assert bad_origin.value.status == 403

            browser = await client.ws_connect(
                "/signal?role=client&room=home", headers=auth_headers
            )
            assert await browser.receive_json() == {"type": "agent_online"}
            await browser.close()
            assert await agent.receive_json() == {"type": "client_offline"}
            await agent.close()
        rooms.clear()

    asyncio.run(scenario())


def test_empty_room_password_does_not_require_login():
    async def scenario():
        rooms.clear()
        private_key = Ed25519PrivateKey.generate()
        app = create_app(
            {"auth": {"public_keys": [public_key_text(private_key.public_key())]}}
        )
        async with TestClient(TestServer(app)) as client:
            agent = await authenticate_agent(client, private_key, "home")
            response = await client.get("/home")
            assert response.status == 200
            assert '"protected":false' in await response.text()
            browser = await client.ws_connect("/signal?role=client&room=home")
            assert await browser.receive_json() == {"type": "agent_online"}
            await browser.close()
            await agent.close()
        rooms.clear()

    asyncio.run(scenario())
