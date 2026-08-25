"""Reusable authenticated WebRTC agent transport."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp
from aiortc import (
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.sdp import candidate_from_sdp

from .auth import (
    auth_payload,
    fingerprint,
    load_private_key,
    load_public_key,
    public_key_bytes,
    public_key_text,
)
from .protocol import http_response, validate_stun_urls

log = logging.getLogger("bifrost.agent")
MessageHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]


async def authenticate(
    sig, room, private_key, public_key, timeout=10, password_hash=""
):
    msg = await sig.receive(timeout=timeout)
    if msg.type != aiohttp.WSMsgType.TEXT:
        raise PermissionError("server closed before authentication")
    try:
        challenge_message = json.loads(msg.data)
        if challenge_message.get("type") != "auth_challenge":
            raise ValueError("unexpected authentication message")
        if challenge_message.get("room") != room:
            raise ValueError("authentication room mismatch")
        challenge = base64.b64decode(challenge_message["challenge"], validate=True)
        signature = private_key.sign(auth_payload(room, challenge, password_hash))
    except (KeyError, TypeError, ValueError) as exc:
        raise PermissionError("invalid authentication challenge") from exc
    await sig.send_json({
        "type": "auth_response",
        "public_key": public_key_text(public_key),
        "signature": base64.b64encode(signature).decode("ascii"),
        "password_hash": password_hash,
    })
    reply = await sig.receive(timeout=timeout)
    if reply.type != aiohttp.WSMsgType.TEXT:
        raise PermissionError("agent public key was rejected")
    try:
        result = json.loads(reply.data)
    except ValueError as exc:
        raise PermissionError("invalid authentication result") from exc
    if result.get("type") != "auth_ok":
        raise PermissionError(result.get("error", "agent public key was rejected"))
    try:
        stun_urls = validate_stun_urls(
            result.get("stun_urls", []), field="server stun_urls"
        )
    except (TypeError, ValueError) as exc:
        raise PermissionError("server returned invalid STUN configuration") from exc
    log.info("authenticated agent key=%s room=%s", fingerprint(public_key), room)
    return stun_urls


def load_identity(auth):
    private_key = load_private_key(auth["private_key"], auth.get("private_key_password"))
    public_key = load_public_key(auth["public_key"])
    if public_key_bytes(private_key.public_key()) != public_key_bytes(public_key):
        raise ValueError("client public_key does not match private_key")
    return private_key, public_key


def rtc_configuration(stun_urls):
    """Build an explicit configuration so an empty list disables aiortc defaults."""
    ice_servers = [RTCIceServer(urls=list(stun_urls))] if stun_urls else []
    return RTCConfiguration(iceServers=ice_servers)


def select_stun_urls(configured_stun_urls, server_stun_urls):
    """Prefer the client configuration and fall back to the server list."""
    return list(configured_stun_urls or server_stun_urls)


async def _dispatch(channel, raw, handler: MessageHandler):
    request_id = None
    try:
        message = json.loads(raw)
        request_id = message.get("id")
        result = await handler(message)
        if result is not None and channel.readyState == "open":
            channel.send(json.dumps(result))
    except Exception as exc:
        log.exception("request forwarding failed")
        if channel.readyState == "open":
            channel.send(json.dumps(http_response(request_id, 502, error=str(exc))))


async def run_agent(cfg, handler: MessageHandler, identity=None):
    """Run one authenticated signaling/WebRTC session using ``handler``."""
    signal = cfg["signal"]
    auth = cfg["auth"]
    password_hash = cfg.get("browser_auth", {}).get("password_hash", "")
    private_key, public_key = identity or load_identity(auth)
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(
            signal["url"],
            params={"role": "agent", "room": signal["room"]},
            ssl=False if not signal.get("verify_tls", True) else None,
            heartbeat=20,
            max_msg_size=8 * 1024 * 1024,
        ) as sig:
            server_stun_urls = await authenticate(
                sig,
                signal["room"],
                private_key,
                public_key,
                auth.get("timeout", 10),
                password_hash,
            )
            configured_stun_urls = validate_stun_urls(
                cfg.get("webrtc", {}).get("stun_urls", [])
            )
            stun_urls = select_stun_urls(configured_stun_urls, server_stun_urls)
            configuration = rtc_configuration(stun_urls)
            log.info(
                "using %s STUN configuration urls=%s",
                "client" if configured_stun_urls else "server",
                stun_urls,
            )
            pc = None
            pending = []

            async def send(message):
                if not sig.closed:
                    await sig.send_json(message)

            async def new_pc():
                nonlocal pc
                if pc:
                    await pc.close()
                pc = RTCPeerConnection(configuration=configuration)

                @pc.on("icecandidate")
                async def ice(candidate):
                    if candidate:
                        await send({
                            "type": "candidate",
                            "candidate": {
                                "candidate": candidate.to_sdp(),
                                "sdpMid": candidate.sdpMid,
                                "sdpMLineIndex": candidate.sdpMLineIndex,
                            },
                        })

                @pc.on("connectionstatechange")
                async def state():
                    log.info(
                        "peer state=%s ice=%s",
                        pc.connectionState,
                        pc.iceConnectionState,
                    )

                @pc.on("datachannel")
                def channel(ch):
                    log.info("datachannel=%s", ch.label)

                    @ch.on("message")
                    def message(raw):
                        asyncio.create_task(_dispatch(ch, raw, handler))

                return pc

            async for msg in sig:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                message = json.loads(msg.data)
                if message.get("type") == "offer":
                    pc = await new_pc()
                    await pc.setRemoteDescription(RTCSessionDescription(**message["sdp"]))
                    for candidate in pending:
                        try:
                            await pc.addIceCandidate(candidate)
                        except Exception:
                            pass
                    pending = []
                    answer = await pc.createAnswer()
                    await pc.setLocalDescription(answer)
                    await send({
                        "type": "answer",
                        "sdp": {
                            "sdp": pc.localDescription.sdp,
                            "type": pc.localDescription.type,
                        },
                    })
                elif message.get("type") == "candidate" and message.get("candidate"):
                    value = message["candidate"]
                    candidate = candidate_from_sdp(value["candidate"])
                    candidate.sdpMid = value.get("sdpMid")
                    candidate.sdpMLineIndex = value.get("sdpMLineIndex")
                    if pc and pc.remoteDescription:
                        await pc.addIceCandidate(candidate)
                    else:
                        pending.append(candidate)
                elif message.get("type") == "client_offline":
                    pending = []
                    if pc:
                        await pc.close()
                        pc = None
            if pc:
                await pc.close()


async def serve_agent(
    cfg,
    handler: MessageHandler,
    reconnect_delay=2,
    max_reconnect_delay=60,
    identity=None,
):
    """Keep an agent registered, reconnecting after transient failures."""
    identity = identity or load_identity(cfg["auth"])
    delay = reconnect_delay
    while True:
        failed = False
        try:
            await run_agent(cfg, handler, identity)
            delay = reconnect_delay
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as exc:
            failed = True
            log.warning(
                "signaling connection failed room=%s; retrying in %s seconds: %s",
                cfg["signal"]["room"],
                delay,
                exc,
            )
        except Exception:
            failed = True
            log.exception(
                "agent session failed room=%s; retrying in %s seconds",
                cfg["signal"]["room"],
                delay,
            )
        await asyncio.sleep(delay)
        if failed:
            delay = min(delay * 2, max_reconnect_delay)
