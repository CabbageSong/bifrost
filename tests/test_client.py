import asyncio
import json

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from bifrost.client import handle_http
from bifrost.protocol import decode_body, encode_body


class Channel:
    def __init__(self):
        self.messages = []

    def send(self, raw):
        self.messages.append(json.loads(raw))


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
