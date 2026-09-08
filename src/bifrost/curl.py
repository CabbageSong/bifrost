"""Curl-like HTTP access through a Bifrost room without a browser."""

from __future__ import annotations

import argparse
import asyncio
import base64
import getpass
import json
import re
import sys
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import BinaryIO

import aiohttp
from aiortc import (
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.sdp import candidate_from_sdp
from yarl import URL

from .protocol import encode_body
from .room_auth import validate_room_name

_METHOD = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_BIFROST_CONFIG = re.compile(r"const\s+bifrostConfig\s*=\s*")


class BifrostCurlError(RuntimeError):
    """An expected error while connecting or forwarding a request."""


@dataclass(frozen=True)
class HTTPResponse:
    """The response returned by the local service through Bifrost."""

    status: int
    reason: str
    headers: dict[str, str]
    body: bytes
    url: str = ""


class _LoginForm(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = {}

    def handle_starttag(self, tag, attrs):
        if tag != "input":
            return
        fields = dict(attrs)
        name = fields.get("name")
        if name:
            self.values[name] = fields.get("value", "")


def _server_url(value):
    server = URL(value)
    if server.scheme not in {"http", "https"} or not server.host:
        raise ValueError("server must be an http:// or https:// URL")
    return server.with_path("").with_query({}).with_fragment("")


def _request_path(value):
    target = URL(value)
    if target.scheme and target.scheme not in {"http", "https"}:
        raise ValueError("request URL must use http:// or https://")
    path = target.raw_path_qs or "/"
    if not path.startswith("/"):
        path = "/" + path
    return path


def _parse_page_config(html):
    match = _BIFROST_CONFIG.search(html)
    if match is None:
        raise BifrostCurlError("room page did not contain Bifrost configuration")
    try:
        config, _ = json.JSONDecoder().raw_decode(html[match.end():].lstrip())
        ice_servers = config["iceServers"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BifrostCurlError("room page contained invalid Bifrost configuration") from exc
    if not isinstance(ice_servers, list):
        raise BifrostCurlError("room page contained invalid ICE configuration")
    return ice_servers


class BifrostHTTPClient:
    """Make HTTP requests through one Bifrost room as a browser would."""

    def __init__(
        self,
        server,
        room,
        *,
        verify_tls=True,
        password=None,
        prompt_password=False,
        timeout=20,
    ):
        self.server = _server_url(server)
        validate_room_name(room)
        self.room = room
        self.verify_tls = verify_tls
        self.password = password
        self.prompt_password = prompt_password
        self.timeout = timeout
        self._session = None
        self._signal = None
        self._signal_task = None
        self._peer = None
        self._channel = None
        self._channel_open = asyncio.Event()
        self._answer_received = asyncio.Event()
        self._pending_candidates = []
        self._responses = {}
        self._request_id = 0
        self._signal_error = None

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, _type, _value, _traceback):
        await self.close()

    async def connect(self):
        """Authenticate, negotiate a WebRTC peer and open the HTTP channel."""
        if self._session is not None:
            return
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        self._session = aiohttp.ClientSession(
            cookie_jar=aiohttp.CookieJar(unsafe=True),
            timeout=aiohttp.ClientTimeout(total=self.timeout),
        )
        try:
            ice_servers = await self._load_room_config()
            self._peer = RTCPeerConnection(
                RTCConfiguration(
                    iceServers=[RTCIceServer(**server) for server in ice_servers]
                )
            )
            self._peer.on("icecandidate", self._send_candidate)
            self._channel = self._peer.createDataChannel("http")
            self._channel.on("open", self._on_channel_open)
            self._channel.on("message", self._on_channel_message)
            signal_url = self.server.with_path("/signal")
            websocket_url = signal_url.with_scheme(
                "wss" if signal_url.scheme == "https" else "ws"
            )
            self._signal = await self._session.ws_connect(
                websocket_url,
                params={"role": "client", "room": self.room},
                ssl=self.verify_tls,
                heartbeat=20,
                max_msg_size=8 * 1024 * 1024,
            )
            first = await self._signal.receive(timeout=self.timeout)
            if first.type != aiohttp.WSMsgType.TEXT:
                raise BifrostCurlError("Bifrost server closed the signaling connection")
            try:
                if json.loads(first.data) != {"type": "agent_online"}:
                    raise BifrostCurlError("Bifrost client is not online for this room")
            except json.JSONDecodeError as exc:
                raise BifrostCurlError("invalid signaling response") from exc

            self._signal_task = asyncio.create_task(self._read_signaling())
            offer = await self._peer.createOffer()
            await self._peer.setLocalDescription(offer)
            await self._signal.send_json({
                "type": "offer",
                "sdp": {
                    "type": self._peer.localDescription.type,
                    "sdp": self._peer.localDescription.sdp,
                },
            })
            await asyncio.wait_for(self._answer_received.wait(), self.timeout)
            if self._signal_error is not None:
                raise self._signal_error
            await asyncio.wait_for(self._channel_open.wait(), self.timeout)
        except BaseException:
            await self.close()
            raise

    async def close(self):
        """Close the DataChannel, signaling WebSocket and HTTP session."""
        for future in self._responses.values():
            if not future.done():
                future.set_exception(BifrostCurlError("Bifrost connection closed"))
        self._responses.clear()
        if self._signal_task is not None:
            self._signal_task.cancel()
            await asyncio.gather(self._signal_task, return_exceptions=True)
            self._signal_task = None
        if self._signal is not None and not self._signal.closed:
            await self._signal.close()
        self._signal = None
        if self._peer is not None:
            await self._peer.close()
        self._peer = None
        self._channel = None
        if self._session is not None:
            await self._session.close()
        self._session = None

    async def request(self, method, path, *, headers=None, body=b""):
        """Forward one HTTP request and return its decoded response."""
        if self._channel is None or self._channel.readyState != "open":
            raise BifrostCurlError("Bifrost WebRTC channel is not open")
        method = method.upper()
        if not _METHOD.fullmatch(method):
            raise ValueError("invalid HTTP method")
        if not isinstance(body, bytes):
            raise TypeError("request body must be bytes")
        self._request_id += 1
        request_id = self._request_id
        loop = asyncio.get_running_loop()
        response_future = loop.create_future()
        self._responses[request_id] = response_future
        try:
            self._channel.send(json.dumps({
                "type": "http_request",
                "id": request_id,
                "method": method,
                "path": _request_path(path),
                "headers": dict(headers or {}),
                "body_base64": encode_body(body),
            }))
            response = await asyncio.wait_for(response_future, self.timeout)
        finally:
            self._responses.pop(request_id, None)
        if response.get("type") != "http_response":
            raise BifrostCurlError("invalid HTTP response from Bifrost client")
        try:
            response_body = base64.b64decode(response["body_base64"], validate=True)
        except (KeyError, ValueError) as exc:
            raise BifrostCurlError("invalid HTTP response body from Bifrost client") from exc
        return HTTPResponse(
            status=response["status"],
            reason=response.get("status_text", ""),
            headers=response.get("headers", {}),
            body=response_body,
            url=response.get("response_url", ""),
        )

    async def _load_room_config(self):
        room_url = self.server.with_path("/" + self.room)
        async with self._session.get(room_url, ssl=self.verify_tls) as response:
            page = await response.text()
        if response.status == 401:
            parser = _LoginForm()
            parser.feed(page)
            required = {"room", "next", "login_token"}
            if not required <= parser.values.keys():
                raise BifrostCurlError("Bifrost room requires login, but no login form was found")
            password = self.password
            if password is None and self.prompt_password:
                password = await asyncio.to_thread(getpass.getpass, "Bifrost room password: ")
            if password is None:
                raise BifrostCurlError(
                    "Bifrost room requires a password; use --password or --password-stdin"
                )
            form = dict(parser.values)
            form["password"] = password
            login_url = self.server.with_path("/_bifrost/login")
            async with self._session.post(
                login_url,
                data=form,
                allow_redirects=False,
                ssl=self.verify_tls,
            ) as login_response:
                if login_response.status not in {301, 302, 303, 307, 308}:
                    detail = (await login_response.text()).strip()
                    raise BifrostCurlError(
                        f"Bifrost room login failed with HTTP {login_response.status}: {detail}"
                    )
            async with self._session.get(room_url, ssl=self.verify_tls) as response:
                page = await response.text()
        if response.status != 200:
            raise BifrostCurlError(f"Bifrost room is unavailable (HTTP {response.status})")
        return _parse_page_config(page)

    async def _read_signaling(self):
        try:
            async for message in self._signal:
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    value = json.loads(message.data)
                    message_type = value.get("type")
                    if message_type == "answer":
                        await self._peer.setRemoteDescription(
                            RTCSessionDescription(**value["sdp"])
                        )
                        for candidate in self._pending_candidates:
                            await self._peer.addIceCandidate(candidate)
                        self._pending_candidates.clear()
                        self._answer_received.set()
                    elif message_type == "candidate" and value.get("candidate"):
                        candidate_value = value["candidate"]
                        candidate = candidate_from_sdp(
                            candidate_value["candidate"].removeprefix("candidate:")
                        )
                        candidate.sdpMid = candidate_value.get("sdpMid")
                        candidate.sdpMLineIndex = candidate_value.get("sdpMLineIndex")
                        if self._peer.remoteDescription:
                            await self._peer.addIceCandidate(candidate)
                        else:
                            self._pending_candidates.append(candidate)
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    self._signal_error = BifrostCurlError("invalid signaling message")
                    self._answer_received.set()
                    return
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, OSError, TimeoutError) as exc:
            self._signal_error = BifrostCurlError(
                f"signaling connection failed: {exc}"
            )
            self._answer_received.set()

    async def _send_candidate(self, candidate):
        if candidate is not None and self._signal is not None and not self._signal.closed:
            await self._signal.send_json({
                "type": "candidate",
                "candidate": candidate.toJSON(),
            })

    def _on_channel_open(self):
        self._channel_open.set()

    def _on_channel_message(self, raw):
        try:
            message = json.loads(raw)
            request_id = message.get("id")
        except (TypeError, ValueError, json.JSONDecodeError):
            return
        response = self._responses.get(request_id)
        if response is not None and not response.done():
            response.set_result(message)


def _parse_headers(values):
    headers = {}
    for value in values:
        name, separator, item = value.partition(":")
        if not separator or not name.strip():
            raise ValueError(f"invalid header: {value!r}")
        headers[name.strip()] = item.lstrip()
    return headers


def _read_data(values, stream: BinaryIO):
    chunks = []
    for value in values:
        if value.startswith("@"):
            filename = value[1:]
            if filename == "-":
                chunks.append(stream.read())
            else:
                chunks.append(Path(filename).read_bytes())
        else:
            chunks.append(value.encode("utf-8"))
    return b"&".join(chunks)


def _write_response(response, output, include):
    if output == "-":
        _write_response_stream(response, sys.stdout.buffer, include)
        return
    with open(output, "wb") as stream:
        _write_response_stream(response, stream, include)


def _write_response_stream(response, stream, include):
    if include:
        status_line = f"HTTP/1.1 {response.status} {response.reason}\r\n"
        stream.write(status_line.encode("iso-8859-1"))
        for name, value in response.headers.items():
            stream.write(f"{name}: {value}\r\n".encode("iso-8859-1"))
        stream.write(b"\r\n")
    stream.write(response.body)
    stream.flush()


def _parser():
    parser = argparse.ArgumentParser(
        prog="bifrost-curl",
        description="Make an HTTP request through a Bifrost room without a browser",
    )
    parser.add_argument("url", help="private path, for example /healthz")
    parser.add_argument("--server", required=True, help="Bifrost server URL")
    parser.add_argument("--room", required=True, help="Bifrost room name")
    parser.add_argument("-X", "--request", dest="method", help="HTTP method")
    parser.add_argument("-H", "--header", action="append", default=[], help="header")
    parser.add_argument("-d", "--data", action="append", default=[], help="request data")
    parser.add_argument(
        "--data-binary", action="append", default=[], help="request data without form encoding"
    )
    parser.add_argument("--password", help="room password")
    parser.add_argument(
        "--password-stdin", action="store_true", help="read the room password from stdin"
    )
    parser.add_argument("-k", "--insecure", action="store_true", help="disable TLS verification")
    parser.add_argument("-i", "--include", action="store_true", help="include response headers")
    parser.add_argument("-o", "--output", default="-", help="write the response body to FILE")
    parser.add_argument("-f", "--fail", action="store_true", help="fail for HTTP errors")
    parser.add_argument("-v", "--verbose", action="store_true", help="print request details")
    parser.add_argument("--timeout", type=float, default=20, help="request timeout in seconds")
    return parser


async def _run(args):
    if args.password is not None and args.password_stdin:
        raise ValueError("--password and --password-stdin cannot be used together")
    password = args.password
    if args.password_stdin:
        password = sys.stdin.buffer.readline().rstrip(b"\r\n").decode("utf-8")
    body = b""
    if args.data:
        body += _read_data(args.data, sys.stdin.buffer)
    if args.data_binary:
        body += _read_data(args.data_binary, sys.stdin.buffer)
    method = args.method or ("POST" if args.data or args.data_binary else "GET")
    headers = _parse_headers(args.header)
    if args.data and not any(name.lower() == "content-type" for name in headers):
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if args.verbose:
        print(f"> {method.upper()} {args.url}", file=sys.stderr)
        for name, value in headers.items():
            print(f"> {name}: {value}", file=sys.stderr)
    async with BifrostHTTPClient(
        args.server,
        args.room,
        verify_tls=not args.insecure,
        password=password,
        prompt_password=True,
        timeout=args.timeout,
    ) as connection:
        response = await connection.request(method, args.url, headers=headers, body=body)
    if args.verbose:
        print(f"< HTTP {response.status} {response.reason}", file=sys.stderr)
        for name, value in response.headers.items():
            print(f"< {name}: {value}", file=sys.stderr)
    _write_response(response, args.output, args.include)
    if args.fail and response.status >= 400:
        return 22
    return 0


def cli():
    parser = _parser()
    args = parser.parse_args()
    try:
        return_code = asyncio.run(_run(args))
    except (BifrostCurlError, OSError, ValueError, aiohttp.ClientError) as exc:
        print(f"bifrost-curl: {exc}", file=sys.stderr)
        return_code = 2
    raise SystemExit(return_code)


if __name__ == "__main__":
    cli()
