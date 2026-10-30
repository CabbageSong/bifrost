"""Proxy one or more Bifrost rooms to local HTTP ports."""

import argparse
import asyncio
import contextlib
import logging
import math
import re

import aiohttp
from yarl import URL

from .agent import load_identity, serve_agent
from .protocol import (
    CHUNK_SIZE,
    decode_body,
    encode_body,
    http_chunk,
    http_response,
    load_config,
    resolve_config_path,
    validate_config_table,
    validate_ice_port,
    validate_ice_servers,
)
from .room_auth import parse_password_hash, validate_room_name

log = logging.getLogger("bifrost.client")
_WEBSOCKET_PROTOCOL = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


def configured_services(cfg):
    """Validate and normalize the room-to-local-port mappings."""
    validate_config_table(
        cfg,
        "client config",
        {"signal", "webrtc", "services", "auth"},
    )
    signal_config = validate_config_table(
        cfg["signal"], "signal", {"url", "verify_tls"}
    )
    if not isinstance(signal_config["url"], str) or not signal_config["url"]:
        raise TypeError("signal.url must be a non-empty string")
    if not isinstance(signal_config["verify_tls"], bool):
        raise TypeError("signal.verify_tls must be a boolean")

    auth = validate_config_table(
        cfg["auth"],
        "auth",
        {"private_key", "timeout"},
    )
    if not isinstance(auth["private_key"], str) or not auth["private_key"]:
        raise TypeError("auth.private_key must be a non-empty string")
    if (
        isinstance(auth["timeout"], bool)
        or not isinstance(auth["timeout"], (int, float))
        or not math.isfinite(auth["timeout"])
        or auth["timeout"] <= 0
    ):
        raise ValueError("auth.timeout must be a positive number")

    webrtc = validate_config_table(cfg["webrtc"], "webrtc", {"ice_servers"})
    ice_servers = validate_ice_servers(webrtc["ice_servers"])
    services = cfg["services"]
    if not isinstance(services, list) or not services:
        raise ValueError("client config requires at least one [[services]] entry")
    base_service_webrtc = {"ice_servers": ice_servers}
    seen_rooms = set()
    seen_ice_ports = set()
    result = []
    for index, service in enumerate(services, 1):
        item_field = f"services entry {index}"
        validate_config_table(
            service,
            item_field,
            {"room", "local_port", "ice_port", "host", "scheme", "password_hash"},
        )
        room = service["room"]
        try:
            validate_room_name(room)
        except ValueError as exc:
            raise ValueError(f"invalid room in services entry {index}: {exc}") from exc
        if room in seen_rooms:
            raise ValueError(f"duplicate room in client config: {room}")
        port = service["local_port"]
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError(f"invalid local_port for room {room}: {port}")
        seen_rooms.add(room)
        ice_port = validate_ice_port(
            service["ice_port"],
            f"services entry {index} ice_port",
        )
        if ice_port in seen_ice_ports:
            raise ValueError(f"duplicate ice_port in client config: {ice_port}")
        seen_ice_ports.add(ice_port)
        scheme = service["scheme"]
        host = service["host"]
        if not isinstance(scheme, str) or scheme.lower() not in {
            "http",
            "https",
        }:
            raise ValueError(f"invalid scheme for room {room}: expected http or https")
        scheme = scheme.lower()
        if not isinstance(host, str) or not host.strip():
            raise ValueError(f"invalid host for room {room}")
        try:
            target = str(URL.build(scheme=scheme, host=host, port=port))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid host for room {room}") from exc
        password_hash = service["password_hash"]
        if password_hash:
            try:
                parse_password_hash(password_hash)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"invalid password_hash for room {room}: {exc}"
                ) from exc
        elif not isinstance(password_hash, str):
            raise ValueError(f"password_hash for room {room} must be a string")
        signal = dict(cfg["signal"])
        signal["room"] = room
        service_webrtc = dict(base_service_webrtc)
        service_webrtc["ice_port"] = ice_port
        service_cfg = {
            "signal": signal,
            "auth": cfg["auth"],
            "browser_auth": {"password_hash": password_hash},
            "webrtc": service_webrtc,
        }
        result.append((room, target, service_cfg))
    return result


class ServiceProxy:
    """Proxy HTTP requests and full-duplex WebSockets for one local service."""

    def __init__(self, target, session):
        self.target = target
        self.session = session
        self.websockets = {}
        self.socket_send_buffers = {}
        self.http_request_buffers = {}

    async def __call__(self, message, reply):
        message_type = message.get("type")
        if message_type == "http_request":
            if message.get("chunked"):
                self.http_request_buffers[message["id"]] = {
                    "header": message,
                    "chunks": [],
                }
                return
            await self._proxy_http(message, reply)
        elif message_type == "http_request_chunk":
            await self._handle_http_request_chunk(message, reply)
        elif message_type == "websocket_open":
            await reply(await self._open_websocket(message, reply))
        elif message_type == "websocket_send":
            await self._send_websocket(message, reply)
        elif message_type == "websocket_send_chunk":
            await self._send_websocket_chunk(message, reply)
        elif message_type == "websocket_close":
            await self._close_websocket(message, reply)
        else:
            raise ValueError("unsupported proxy message type")

    async def _handle_http_request_chunk(self, message, reply):
        req_id = message.get("id")
        entry = self.http_request_buffers.get(req_id)
        if not entry:
            return
        body = message.get("body")
        if body is None:
            data = message.get("data")
            if isinstance(data, (bytes, bytearray, memoryview)):
                body = bytes(data)
            elif isinstance(data, str):
                body = decode_body(data)
            else:
                body = b""
        elif not isinstance(body, (bytes, bytearray, memoryview)):
            body = bytes(body)
        entry["chunks"].append(bytes(body))
        if message.get("eof"):
            self.http_request_buffers.pop(req_id, None)
            full_body = b"".join(entry["chunks"])
            req_msg = entry["header"]
            req_msg["body"] = full_body
            await self._proxy_http(req_msg, reply)

    async def _proxy_http(self, message, reply):
        request_id = message.get("id")
        try:
            path = message.get("path") or "/"
            path = path if path.startswith("/") else "/" + path
            headers = {
                key: value
                for key, value in message.get("headers", {}).items()
                if key.lower() not in ("host", "content-length")
            }
            if "body" in message and isinstance(
                message["body"], (bytes, bytearray, memoryview)
            ):
                body = bytes(message["body"])
            elif "body_base64" in message:
                body = decode_body(message["body_base64"])
            else:
                body = b""
            method = (message.get("method") or "GET").upper()
            async with self.session.request(
                method,
                self.target.rstrip("/") + path,
                headers=headers,
                data=body or None,
            ) as response:
                raw_body = await response.read()
                if len(raw_body) <= CHUNK_SIZE:
                    await reply(
                        http_response(
                            request_id,
                            response.status,
                            dict(response.headers),
                            body=raw_body,
                            status_text=response.reason or "",
                            response_url=response.url.raw_path_qs,
                        )
                    )
                    return

                await reply(
                    http_response(
                        request_id,
                        response.status,
                        dict(response.headers),
                        body=b"",
                        status_text=response.reason or "",
                        response_url=response.url.raw_path_qs,
                        chunked=True,
                        total_size=len(raw_body),
                    )
                )
                for i in range(0, len(raw_body), CHUNK_SIZE):
                    chunk = raw_body[i : i + CHUNK_SIZE]
                    is_last = (i + CHUNK_SIZE >= len(raw_body))
                    seq = i // CHUNK_SIZE
                    await reply(
                        http_chunk(
                            request_id,
                            seq,
                            eof=is_last,
                            body=chunk,
                        )
                    )
                    if seq % 8 == 0:
                        await asyncio.sleep(0.001)
        except Exception as exc:
            await reply(http_response(request_id, 502, {}, body=b"", error=str(exc)))

    def _socket_key(self, message, reply):
        socket_id = message.get("socket_id")
        if not isinstance(socket_id, str) or not socket_id or len(socket_id) > 200:
            raise ValueError("invalid websocket socket_id")
        return id(reply), socket_id

    async def _open_websocket(self, message, reply):
        key = self._socket_key(message, reply)
        path = message.get("path") or "/"
        if (
            not isinstance(path, str)
            or not path.startswith("/")
            or path.startswith("//")
            or "#" in path
            or any(ord(character) < 32 for character in path)
        ):
            raise ValueError("invalid websocket path")
        protocols = message.get("protocols") or []
        if (
            not isinstance(protocols, list)
            or len(protocols) > 32
            or any(
                not isinstance(value, str)
                or len(value) > 200
                or _WEBSOCKET_PROTOCOL.fullmatch(value) is None
                for value in protocols
            )
            or len(set(protocols)) != len(protocols)
        ):
            raise ValueError("invalid websocket protocols")

        previous = self.websockets.pop(key, None)
        if previous is not None:
            await previous[0].close()
        target = URL(self.target)
        target = target.with_scheme("wss" if target.scheme == "https" else "ws")
        url = str(target).rstrip("/") + path
        try:
            websocket = await self.session.ws_connect(
                url,
                origin=str(URL(self.target).origin()),
                protocols=protocols,
                max_msg_size=8 * 1024 * 1024,
            )
        except (aiohttp.ClientError, OSError, TimeoutError) as exc:
            return {
                "type": "websocket_open_failed",
                "socket_id": key[1],
                "error": str(exc),
            }

        task = asyncio.create_task(
            self._relay_websocket(key, websocket, reply),
            name=f"bifrost-websocket-{key[1]}",
        )
        self.websockets[key] = websocket, task
        return {
            "type": "websocket_opened",
            "socket_id": key[1],
            "protocol": websocket.protocol or "",
        }

    async def _relay_websocket(self, key, websocket, reply):
        try:
            async for message in websocket:
                if message.type == aiohttp.WSMsgType.TEXT:
                    raw_text = message.data
                    raw_bytes = raw_text.encode("utf-8")
                    if len(raw_bytes) <= CHUNK_SIZE:
                        await reply({
                            "type": "websocket_message",
                            "socket_id": key[1],
                            "data_type": "text",
                            "data": raw_text,
                        })
                    else:
                        for i in range(0, len(raw_bytes), CHUNK_SIZE):
                            chunk = raw_bytes[i : i + CHUNK_SIZE]
                            is_last = (i + CHUNK_SIZE >= len(raw_bytes))
                            seq = i // CHUNK_SIZE
                            await reply({
                                "type": "websocket_message_chunk",
                                "socket_id": key[1],
                                "data_type": "text",
                                "seq": seq,
                                "eof": is_last,
                                "body": chunk,
                            })
                            if seq % 8 == 0:
                                await asyncio.sleep(0.001)
                elif message.type == aiohttp.WSMsgType.BINARY:
                    raw_bytes = bytes(message.data)
                    if len(raw_bytes) <= CHUNK_SIZE:
                        await reply({
                            "type": "websocket_message",
                            "socket_id": key[1],
                            "data_type": "binary",
                            "body": raw_bytes,
                        })
                    else:
                        for i in range(0, len(raw_bytes), CHUNK_SIZE):
                            chunk = raw_bytes[i : i + CHUNK_SIZE]
                            is_last = (i + CHUNK_SIZE >= len(raw_bytes))
                            seq = i // CHUNK_SIZE
                            await reply({
                                "type": "websocket_message_chunk",
                                "socket_id": key[1],
                                "data_type": "binary",
                                "seq": seq,
                                "eof": is_last,
                                "body": chunk,
                            })
                            if seq % 8 == 0:
                                await asyncio.sleep(0.001)
                elif message.type == aiohttp.WSMsgType.ERROR:
                    await reply({
                        "type": "websocket_error",
                        "socket_id": key[1],
                    })
                    break
        except (ConnectionError, asyncio.CancelledError):
            raise
        except Exception:
            log.exception("local websocket relay failed")
            with contextlib.suppress(ConnectionError):
                await reply({"type": "websocket_error", "socket_id": key[1]})
        finally:
            current = self.websockets.get(key)
            if current is not None and current[0] is websocket:
                self.websockets.pop(key, None)
            self.socket_send_buffers.pop(key, None)
            if not websocket.closed:
                await websocket.close()
            with contextlib.suppress(ConnectionError):
                await reply({
                    "type": "websocket_closed",
                    "socket_id": key[1],
                    "code": websocket.close_code or 1006,
                    "reason": "",
                })

    async def _send_websocket(self, message, reply):
        key = self._socket_key(message, reply)
        entry = self.websockets.get(key)
        if entry is None or entry[0].closed:
            self.socket_send_buffers.pop(key, None)
            raise ConnectionError("websocket is not open")
        data_type = message.get("data_type")
        if data_type == "text" and isinstance(message.get("data"), str):
            await entry[0].send_str(message["data"])
        elif data_type == "binary":
            if "body" in message and isinstance(
                message["body"], (bytes, bytearray, memoryview)
            ):
                payload = bytes(message["body"])
            elif isinstance(message.get("data"), (bytes, bytearray, memoryview)):
                payload = bytes(message["data"])
            elif isinstance(message.get("data"), str):
                payload = decode_body(message["data"])
            else:
                raise ValueError("invalid websocket binary data")
            await entry[0].send_bytes(payload)
        else:
            raise ValueError("invalid websocket message")

    async def _send_websocket_chunk(self, message, reply):
        key = self._socket_key(message, reply)
        entry = self.websockets.get(key)
        if entry is None or entry[0].closed:
            self.socket_send_buffers.pop(key, None)
            raise ConnectionError("websocket is not open")
        body = message.get("body")
        if body is None:
            data = message.get("data")
            if isinstance(data, (bytes, bytearray, memoryview)):
                body = bytes(data)
            elif isinstance(data, str):
                body = decode_body(data)
            else:
                body = b""
        elif not isinstance(body, (bytes, bytearray, memoryview)):
            body = bytes(body)
        buf = self.socket_send_buffers.setdefault(key, [])
        buf.append(bytes(body))
        if message.get("eof"):
            self.socket_send_buffers.pop(key, None)
            payload = b"".join(buf)
            data_type = message.get("data_type")
            if data_type == "text":
                await entry[0].send_str(payload.decode("utf-8"))
            elif data_type == "binary":
                await entry[0].send_bytes(payload)
            else:
                raise ValueError("invalid websocket message data_type")

    async def _close_websocket(self, message, reply):
        key = self._socket_key(message, reply)
        self.socket_send_buffers.pop(key, None)
        entry = self.websockets.get(key)
        if entry is None:
            return
        code = message.get("code", 1000)
        reason = message.get("reason", "")
        if (
            isinstance(code, bool)
            or not isinstance(code, int)
            or code not in {1000, 1001}
            and not 3000 <= code <= 4999
        ):
            raise ValueError("invalid websocket close code")
        if not isinstance(reason, str) or len(reason.encode("utf-8")) > 123:
            raise ValueError("invalid websocket close reason")
        await entry[0].close(code=code, message=reason.encode("utf-8"))

    async def close_connection(self, reply):
        self.socket_send_buffers.clear()
        self.http_request_buffers.clear()
        owner = id(reply)
        entries = [
            (key, websocket, task)
            for key, (websocket, task) in self.websockets.items()
            if key[0] == owner
        ]
        for key, websocket, _task in entries:
            self.websockets.pop(key, None)
            await websocket.close()
        for _key, _websocket, task in entries:
            task.cancel()
        if entries:
            await asyncio.gather(
                *(task for _key, _websocket, task in entries),
                return_exceptions=True,
            )


async def run(cfg):
    services = configured_services(cfg)
    identity = load_identity(cfg["auth"])

    async def run_service(room, target, service_cfg):
        async with aiohttp.ClientSession(
            cookie_jar=aiohttp.CookieJar(unsafe=True),
            auto_decompress=False,
        ) as session:
            log.info("registering room=%s target=%s", room, target)
            await serve_agent(
                service_cfg,
                ServiceProxy(target, session),
                identity=identity,
            )

    await asyncio.gather(*(
        run_service(room, target, service_cfg)
        for room, target, service_cfg in services
    ))


def cli():
    parser = argparse.ArgumentParser(description="Run the Bifrost HTTP sidecar")
    parser.add_argument(
        "--config",
        help="path to TOML configuration (default: ~/.config/bifrost/client.toml)",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    try:
        config_path = resolve_config_path(args.config, "client")
    except FileNotFoundError as exc:
        parser.error(str(exc))
    asyncio.run(run(load_config(config_path)))


if __name__ == "__main__":
    cli()
