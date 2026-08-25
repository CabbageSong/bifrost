import argparse
import asyncio
import base64
import binascii
import json
import logging
import os
import ssl
from importlib.resources import files

from aiohttp import WSMsgType, web

from .auth import load_authorized_keys, parse_public_key, public_key_bytes, verify_signature
from .protocol import load_config

log = logging.getLogger("bifrost.server")
rooms = {}
AUTHORIZED_KEYS = web.AppKey("authorized_keys", dict)
AUTH_TIMEOUT = web.AppKey("auth_timeout", float)
DEFAULT_ROOM = web.AppKey("default_room", str)

SHELL = files("bifrost").joinpath("static/index.html").read_text(encoding="utf-8")


async def page(request):
    # The public URL is /<room>; any remaining path is private-service state.
    path = request.path
    if path == "/":
        room, private_path = request.app[DEFAULT_ROOM], "/"
    else:
        parts = path.lstrip("/").split("/", 1)
        room = parts[0] or request.app[DEFAULT_ROOM]
        private_path = "/" + parts[1] if len(parts) == 2 and parts[1] else "/"
    if room in ("signal", "server-healthz"):
        return web.Response(status=404, text="room not found")
    config = json.dumps({"room": room, "path": private_path}, ensure_ascii=True)
    shell = SHELL.replace("/* BIFROST_CONFIG */", config, 1)
    return web.Response(
        text=shell,
        content_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


async def health(request):
    return web.Response(text="ok\n")


async def authenticate_agent(request, ws, room):
    challenge = os.urandom(32)
    await ws.send_json(
        {
            "type": "auth_challenge",
            "algorithm": "ssh-ed25519",
            "room": room,
            "challenge": base64.b64encode(challenge).decode("ascii"),
        }
    )
    try:
        msg = await ws.receive(timeout=request.app[AUTH_TIMEOUT])
        if msg.type != WSMsgType.TEXT:
            raise PermissionError("authentication response not received")
        response = json.loads(msg.data)
        if response.get("type") != "auth_response":
            raise PermissionError("unexpected authentication response")
        public_key = parse_public_key(response["public_key"])
        entry = request.app[AUTHORIZED_KEYS].get(public_key_bytes(public_key))
        signature = base64.b64decode(response["signature"], validate=True)
        if entry is None or not verify_signature(entry, room, challenge, signature):
            raise PermissionError("public key is not authorized for this room")
    except (
        asyncio.TimeoutError,
        binascii.Error,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        PermissionError,
    ) as exc:
        peer = request.remote or "-"
        log.warning("agent authentication failed room=%s peer=%s: %s", room, peer, exc)
        if not ws.closed:
            await ws.send_json({"type": "auth_error", "error": "authentication failed"})
            await ws.close(code=1008, message=b"authentication failed")
        return None
    await ws.send_json({"type": "auth_ok"})
    return entry


async def signal(request):
    role = request.query.get("role")
    room = request.query.get("room", "").strip()
    if role not in ("client", "agent"):
        return web.Response(status=400, text="bad role")
    if not room:
        return web.Response(status=400, text="missing room")

    if role == "client":
        state = rooms.get(room)
        if not state or not state["agent"] or state["agent"].closed:
            return web.Response(status=409, text="room not ready")

    ws = web.WebSocketResponse(max_msg_size=8 * 1024 * 1024)
    await ws.prepare(request)
    identity = None
    if role == "agent":
        identity = await authenticate_agent(request, ws, room)
        if identity is None:
            return ws
        state = rooms.setdefault(room, {"agent": None, "clients": set()})
        if state["agent"] and not state["agent"].closed:
            await ws.close(code=1013, message=b"agent already connected")
            return ws
        state["agent"] = ws
        log.info("agent connected room=%s key=%s", room, identity.fingerprint)
    else:
        for old in list(state["clients"]):
            if not old.closed:
                await old.close(code=1012, message=b"replaced")
        state["clients"].clear()
        state["clients"].add(ws)
        log.info("client connected room=%s", room)
        await ws.send_json({"type": "agent_online"})

    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except ValueError:
                continue
            target = (
                state["agent"]
                if role == "client"
                else next((item for item in state["clients"] if not item.closed), None)
            )
            if target and not target.closed:
                await target.send_json(data)
    finally:
        if role == "agent" and state.get("agent") is ws:
            state["agent"] = None
        state["clients"].discard(ws)
        if not state["agent"] and not state["clients"] and rooms.get(room) is state:
            rooms.pop(room)
        log.info("%s disconnected room=%s", role, room)
    return ws


def create_app(cfg):
    auth = cfg["auth"]
    app = web.Application()
    app[AUTHORIZED_KEYS] = load_authorized_keys(auth["public_keys"])
    app[AUTH_TIMEOUT] = float(auth.get("timeout", 10))
    app[DEFAULT_ROOM] = cfg.get("server", {}).get("room", "home")
    app.router.add_get("/signal", signal)
    app.router.add_get("/server-healthz", health)
    app.router.add_get("/", page)
    app.router.add_get("/{tail:.*}", page)
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    cfg = load_config(args.config)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app = create_app(cfg)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cfg["tls"]["cert"], cfg["tls"]["key"])
    web.run_app(
        app,
        host=cfg["server"]["bind"],
        port=cfg["server"]["port"],
        ssl_context=context,
    )


if __name__ == "__main__":
    main()
