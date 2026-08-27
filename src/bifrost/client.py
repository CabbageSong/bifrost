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
    decode_body,
    encode_body,
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

    async def __call__(self, message, reply):
        message_type = message.get("type")
        if message_type == "http_request":
            await reply(await self._proxy_http(message))
        elif message_type == "websocket_open":
            await reply(await self._open_websocket(message, reply))
        elif message_type == "websocket_send":
            await self._send_websocket(message, reply)
        elif message_type == "websocket_close":
            await self._close_websocket(message, reply)
        else:
            raise ValueError("unsupported proxy message type")

    async def _proxy_http(self, message):
        request_id = message.get("id")
        try:
            path = message.get("path") or "/"
            path = path if path.startswith("/") else "/" + path
            headers = {
                key: value
                for key, value in message.get("headers", {}).items()
                if key.lower() not in ("host", "content-length")
            }
            body = decode_body(message["body_base64"])
            method = (message.get("method") or "GET").upper()
            async with self.session.request(
                method,
                self.target.rstrip("/") + path,
                headers=headers,
                data=body or None,
            ) as response:
                raw_body = await response.read()
                return http_response(
                    request_id,
                    response.status,
                    dict(response.headers),
                    encode_body(raw_body),
                    status_text=response.reason or "",
                    response_url=response.url.raw_path_qs,
                )
        except Exception as exc:
            return http_response(request_id, 502, {}, "", error=str(exc))

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
                    await reply({
                        "type": "websocket_message",
                        "socket_id": key[1],
                        "data_type": "text",
                        "data": message.data,
                    })
                elif message.type == aiohttp.WSMsgType.BINARY:
                    await reply({
                        "type": "websocket_message",
                        "socket_id": key[1],
                        "data_type": "binary",
                        "data": encode_body(message.data),
                    })
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
            raise ConnectionError("websocket is not open")
        data_type = message.get("data_type")
        if data_type == "text" and isinstance(message.get("data"), str):
            await entry[0].send_str(message["data"])
        elif data_type == "binary" and isinstance(message.get("data"), str):
            await entry[0].send_bytes(decode_body(message["data"]))
        else:
            raise ValueError("invalid websocket message")

    async def _close_websocket(self, message, reply):
        key = self._socket_key(message, reply)
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
            cookie_jar=aiohttp.CookieJar(unsafe=True)
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
