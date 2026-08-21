"""Reusable authenticated WebRTC agent transport."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.sdp import candidate_from_sdp

from .auth import (
    auth_payload,
    fingerprint,
    load_private_key,
    load_public_key,
    public_key_bytes,
    public_key_text,
)
from .protocol import http_response

log = logging.getLogger("bifrost.agent")
MessageHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]


async def authenticate(sig, room, private_key, public_key, timeout=10):
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
        signature = private_key.sign(auth_payload(room, challenge))
    except (KeyError, TypeError, ValueError) as exc:
        raise PermissionError("invalid authentication challenge") from exc
    await sig.send_json({
        "type": "auth_response",
        "public_key": public_key_text(public_key),
        "signature": base64.b64encode(signature).decode("ascii"),
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
    log.info("authenticated agent key=%s room=%s", fingerprint(public_key), room)


def load_identity(auth):
    private_key = load_private_key(auth["private_key"], auth.get("private_key_password"))
    public_key = load_public_key(auth["public_key"])
    if public_key_bytes(private_key.public_key()) != public_key_bytes(public_key):
        raise ValueError("client public_key does not match private_key")
    return private_key, public_key


async def _dispatch(channel, raw, handler: MessageHandler):
    request_id = None
    try:
        message = json.loads(raw)
        request_id = message.get("id")
        result = await handler(message)
        if result is not None and channel.readyState == "open":
            channel.send(json.dumps(result))
    except Exception as exc:
        log.exception("embedded request failed")
        if channel.readyState == "open":
            channel.send(json.dumps(http_response(request_id, 502, error=str(exc))))


async def run_agent(cfg, handler: MessageHandler):
    """Run one authenticated signaling/WebRTC session using ``handler``."""
    signal = cfg["signal"]
    auth = cfg["auth"]
    private_key, public_key = load_identity(auth)
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(
            signal["url"],
            params={"role": "agent", "room": signal["room"]},
            ssl=False if not signal.get("verify_tls", True) else None,
            heartbeat=20,
            max_msg_size=8 * 1024 * 1024,
        ) as sig:
            await authenticate(
                sig, signal["room"], private_key, public_key, auth.get("timeout", 10)
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
                pc = RTCPeerConnection()

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
            if pc:
                await pc.close()


async def serve_agent(cfg, handler: MessageHandler, reconnect_delay=2):
    """Keep an agent registered, reconnecting after transient failures."""
    while True:
        try:
            await run_agent(cfg, handler)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("agent loop stopped")
        await asyncio.sleep(reconnect_delay)
