"""Sidecar mode: proxy DataChannel HTTP messages to a local HTTP server."""

import argparse
import asyncio
import base64
import logging

import aiohttp

from .agent import serve_agent
from .protocol import http_response, load_config

log = logging.getLogger("bifrost.client")


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
            target.rstrip("/") + path,
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
    target = cfg["local_http"]["target"]
    async with aiohttp.ClientSession() as session:
        await serve_agent(cfg, lambda message: proxy_http(message, target, session))


def cli():
    parser = argparse.ArgumentParser(description="Run the Bifrost HTTP sidecar")
    parser.add_argument("--config", required=True, help="path to TOML configuration")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run(load_config(args.config)))


if __name__ == "__main__":
    cli()
