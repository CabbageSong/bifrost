"""Proxy one or more Bifrost rooms to local HTTP ports."""

import argparse
import asyncio
import base64
import logging

import aiohttp

from .agent import load_identity, serve_agent
from .protocol import http_response, load_config

log = logging.getLogger("bifrost.client")


def configured_services(cfg):
    """Validate and normalize the room-to-local-port mappings."""
    services = cfg.get("services")
    if not isinstance(services, list) or not services:
        raise ValueError("client config requires at least one [[services]] entry")
    local = cfg.get("local_http", {})
    scheme = local.get("scheme", "http")
    host = local.get("host", "127.0.0.1")
    seen_rooms = set()
    result = []
    for index, service in enumerate(services, 1):
        room = str(service.get("room", "")).strip()
        if not room:
            raise ValueError(f"services entry {index} requires room")
        if room in seen_rooms:
            raise ValueError(f"duplicate room in client config: {room}")
        try:
            port = int(service["local_port"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"services entry {index} requires local_port") from exc
        if not 1 <= port <= 65535:
            raise ValueError(f"invalid local_port for room {room}: {port}")
        seen_rooms.add(room)
        signal = dict(cfg["signal"])
        signal["room"] = room
        service_cfg = {"signal": signal, "auth": cfg["auth"]}
        result.append((room, f"{scheme}://{host}:{port}", service_cfg))
    return result


async def proxy_http(message, target, session):
    request_id = message.get("id")
    if message.get("type") != "http_request":
        return None
    path = message.get("path") or "/"
    path = path if path.startswith("/") else "/" + path
    headers = {
        key: value
        for key, value in message.get("headers", {}).items()
        if key.lower() not in ("host", "content-length")
    }
    body = (
        base64.b64decode(message["body_base64"])
        if "body_base64" in message
        else message.get("body", "").encode()
    )
    try:
        async with session.request(
            message.get("method", "GET"),
            target + path,
            headers=headers,
            data=body,
        ) as response:
            raw_body = await response.read()
            try:
                text_body = raw_body.decode("utf-8")
                return http_response(
                    request_id, response.status, dict(response.headers), text_body
                )
            except UnicodeDecodeError:
                result = http_response(
                    request_id, response.status, dict(response.headers), ""
                )
                result["body_base64"] = base64.b64encode(raw_body).decode("ascii")
                return result
    except Exception as exc:
        return http_response(request_id, 502, error=str(exc))


async def run(cfg):
    services = configured_services(cfg)
    identity = load_identity(cfg["auth"])
    async with aiohttp.ClientSession() as session:
        tasks = []
        for room, target, service_cfg in services:
            log.info("registering room=%s target=%s", room, target)

            async def handler(message, target=target):
                return await proxy_http(message, target, session)

            tasks.append(serve_agent(service_cfg, handler, identity=identity))
        await asyncio.gather(*tasks)


def cli():
    parser = argparse.ArgumentParser(description="Run the Bifrost HTTP sidecar")
    parser.add_argument("--config", required=True, help="path to TOML configuration")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run(load_config(args.config)))


if __name__ == "__main__":
    cli()
