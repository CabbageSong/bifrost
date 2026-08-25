import argparse
import asyncio
import base64
import binascii
import html
import json
import logging
import math
import os
import ssl
import time
from collections import defaultdict, deque
from importlib.resources import files
from urllib.parse import urlsplit

from aiohttp import WSMsgType, web

from .auth import (
    load_authorized_keys,
    parse_public_key,
    public_key_bytes,
    verify_legacy_signature,
    verify_signature,
)
from .protocol import load_config
from .room_auth import (
    create_login_token,
    create_session_token,
    parse_password_hash,
    session_cookie_name,
    validate_room_name,
    verify_login_token,
    verify_password,
    verify_session_token,
)

log = logging.getLogger("bifrost.server")
rooms = {}
AUTHORIZED_KEYS = web.AppKey("authorized_keys", dict)
AUTH_TIMEOUT = web.AppKey("auth_timeout", float)
DEFAULT_ROOM = web.AppKey("default_room", str)
SESSION_SECRET = web.AppKey("room_session_secret", bytes)
SESSION_TTL = web.AppKey("room_session_ttl", int)
LOGIN_LIMITER = web.AppKey("room_login_limiter", object)
PASSWORD_WORKERS = web.AppKey("room_password_workers", asyncio.Semaphore)

STATIC = files("bifrost").joinpath("static")
SHELL = STATIC.joinpath("index.html").read_text(encoding="utf-8")
PORTAL = STATIC.joinpath("portal.html").read_text(encoding="utf-8")
LOGIN = STATIC.joinpath("login.html").read_text(encoding="utf-8")


class LoginLimiter:
    """Small in-memory limiter that runs before the expensive password KDF."""

    def __init__(self, attempts: int, window: int):
        self.attempts = attempts
        self.window = window
        self.failures = defaultdict(deque)

    def _recent(self, key, now):
        failures = self.failures[key]
        while failures and failures[0] <= now - self.window:
            failures.popleft()
        if not failures:
            self.failures.pop(key, None)
            return ()
        return failures

    def retry_after(self, key, now=None):
        now = time.monotonic() if now is None else now
        failures = self._recent(key, now)
        if len(failures) < self.attempts:
            return 0
        return max(1, math.ceil(self.window - (now - failures[0])))

    def failed(self, key, now=None):
        now = time.monotonic() if now is None else now
        failures = self._recent(key, now)
        if not failures:
            failures = self.failures[key]
        failures.append(now)

    def clear(self, key):
        self.failures.pop(key, None)


def _security_headers(*, frame_options=True):
    headers = {
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }
    if frame_options:
        headers.update({
            "Content-Security-Policy": (
                "default-src 'none'; style-src 'unsafe-inline'; "
                "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
            ),
            "X-Frame-Options": "DENY",
        })
    return headers


def _shell_headers():
    headers = _security_headers(frame_options=False)
    headers.update({
        "Content-Security-Policy": "frame-ancestors 'none'",
        "X-Frame-Options": "DENY",
    })
    return headers


def _html_response(text, *, status=200, headers=None):
    return web.Response(
        text=text,
        status=status,
        content_type="text/html",
        headers=headers or _security_headers(),
    )


def _portal_page(default_room, error="", status=200):
    page = PORTAL.replace("{{DEFAULT_ROOM}}", html.escape(default_room, quote=True))
    page = page.replace("{{ERROR}}", html.escape(error))
    return _html_response(page, status=status)


def _login_page(request, room, next_url, error="", status=401, extra_headers=None):
    login_token = create_login_token(request.app[SESSION_SECRET], room, next_url)
    page = LOGIN.replace("{{ROOM}}", html.escape(room))
    page = page.replace("{{ROOM_VALUE}}", html.escape(room, quote=True))
    page = page.replace("{{NEXT}}", html.escape(next_url, quote=True))
    page = page.replace("{{LOGIN_TOKEN}}", html.escape(login_token, quote=True))
    page = page.replace("{{ERROR}}", html.escape(error))
    headers = _security_headers()
    if extra_headers:
        headers.update(extra_headers)
    return _html_response(page, status=status, headers=headers)


def _json_for_html(value):
    return (
        json.dumps(value, ensure_ascii=True, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def _room_state(room):
    state = rooms.get(room)
    if not state or not state.get("agent") or state["agent"].closed:
        return None
    return state


def _session_expiry(request, room, password_hash):
    token = request.cookies.get(session_cookie_name(room, secure=request.secure))
    if not token:
        return None
    return verify_session_token(
        request.app[SESSION_SECRET], token, room, password_hash
    )


def _safe_next_url(room, value):
    fallback = f"/{room}"
    if not isinstance(value, str):
        return fallback
    try:
        target = urlsplit(value)
    except ValueError:
        return fallback
    room_root = f"/{room}"
    if (
        target.scheme
        or target.netloc
        or not (target.path == room_root or target.path.startswith(room_root + "/"))
    ):
        return fallback
    result = target.path
    if target.query:
        result += "?" + target.query
    return result


async def portal(request):
    requested_room = request.query.get("room", "").strip()
    if requested_room:
        try:
            validate_room_name(requested_room)
        except ValueError as exc:
            return _portal_page(request.app[DEFAULT_ROOM], str(exc), status=400)
        raise web.HTTPFound(f"/{requested_room}")
    return _portal_page(request.app[DEFAULT_ROOM])


async def page(request):
    tail = request.match_info["tail"]
    parts = tail.split("/", 1)
    room = parts[0]
    try:
        validate_room_name(room)
    except ValueError:
        return web.Response(status=404, text="room not found")
    state = _room_state(room)
    if state is None:
        return web.Response(
            status=404,
            text="room is not available\n",
            headers={"Cache-Control": "no-store"},
        )

    password_hash = state["password_hash"]
    protected = bool(password_hash)
    if protected and _session_expiry(request, room, password_hash) is None:
        return _login_page(request, room, str(request.rel_url))

    private_path = "/" + parts[1] if len(parts) == 2 and parts[1] else "/"
    if request.query_string:
        private_path += "?" + request.query_string
    config = _json_for_html(
        {"room": room, "path": private_path, "protected": protected}
    )
    shell = SHELL.replace("/* BIFROST_CONFIG */", config, 1)
    return _html_response(
        shell,
        headers=_shell_headers(),
    )


async def login(request):
    form = await request.post()
    room = form.get("room", "")
    password = form.get("password", "")
    next_url = form.get("next", "")
    login_token = form.get("login_token", "")
    if not isinstance(room, str) or not isinstance(password, str):
        return web.Response(status=400, text="invalid login request")
    try:
        validate_room_name(room)
    except ValueError:
        return web.Response(status=400, text="invalid login request")

    destination = _safe_next_url(room, next_url)
    if not isinstance(login_token, str) or not verify_login_token(
        request.app[SESSION_SECRET], login_token, room, destination
    ):
        return web.Response(status=400, text="invalid or expired login form")

    state = _room_state(room)
    if state is None:
        return _login_page(
            request,
            room,
            f"/{room}",
            "Room 当前不可用，请确认内网 client 已连接。",
            status=404,
        )
    password_hash = state["password_hash"]
    if not password_hash:
        raise web.HTTPFound(destination)

    limiter = request.app[LOGIN_LIMITER]
    limit_key = (room, request.remote or "-")
    retry_after = limiter.retry_after(limit_key)
    if retry_after:
        return _login_page(
            request,
            room,
            destination,
            f"尝试次数过多，请在 {retry_after} 秒后重试。",
            status=429,
            extra_headers={"Retry-After": str(retry_after)},
        )

    async with request.app[PASSWORD_WORKERS]:
        valid = await asyncio.to_thread(verify_password, password_hash, password)
    if not valid:
        limiter.failed(limit_key)
        return _login_page(
            request,
            room,
            destination,
            "密码不正确，请重新输入。",
            status=401,
        )

    limiter.clear(limit_key)
    token, _ = create_session_token(
        request.app[SESSION_SECRET],
        room,
        password_hash,
        request.app[SESSION_TTL],
    )
    response = web.HTTPFound(destination)
    response.headers.update(_security_headers())
    response.set_cookie(
        session_cookie_name(room, secure=request.secure),
        token,
        max_age=request.app[SESSION_TTL],
        secure=request.secure,
        httponly=True,
        samesite="Lax",
        path="/",
    )
    raise response


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
        has_password_policy = "password_hash" in response
        password_hash = response.get("password_hash", "")
        if not isinstance(password_hash, str):
            raise TypeError("invalid room password hash")
        if password_hash:
            parse_password_hash(password_hash)
        public_key = parse_public_key(response["public_key"])
        entry = request.app[AUTHORIZED_KEYS].get(public_key_bytes(public_key))
        signature = base64.b64decode(response["signature"], validate=True)
        valid_signature = entry is not None and verify_signature(
            entry, room, challenge, signature, password_hash
        )
        if (
            not valid_signature
            and entry is not None
            and not has_password_policy
        ):
            valid_signature = verify_legacy_signature(
                entry, room, challenge, signature
            )
        if not valid_signature:
            raise PermissionError("public key is not authorized for this room")
    except (
        TimeoutError,
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
    return entry, password_hash


async def _expire_client_session(ws, expires):
    delay = max(0, expires - time.time())
    await asyncio.sleep(delay)
    if not ws.closed:
        await ws.close(code=1008, message=b"room session expired")


async def _tell_agent_client_offline(state):
    agent = state.get("agent")
    if agent and not agent.closed:
        await agent.send_json({"type": "client_offline"})


async def signal(request):
    role = request.query.get("role")
    room = request.query.get("room", "").strip()
    if role not in ("client", "agent"):
        return web.Response(status=400, text="bad role")
    if not room:
        return web.Response(status=400, text="missing room")
    try:
        validate_room_name(room)
    except ValueError:
        return web.Response(status=400, text="invalid room")

    session_expires = None
    if role == "client":
        origin = request.headers.get("Origin")
        if origin:
            try:
                origin_parts = urlsplit(origin)
            except ValueError:
                return web.Response(status=403, text="invalid websocket origin")
            if (
                origin_parts.scheme not in ("http", "https")
                or origin_parts.netloc != request.host
            ):
                return web.Response(status=403, text="invalid websocket origin")
        state = _room_state(room)
        if state is None:
            return web.Response(status=409, text="room not ready")
        password_hash = state["password_hash"]
        if password_hash:
            session_expires = _session_expiry(request, room, password_hash)
            if session_expires is None:
                return web.Response(status=401, text="room login required")

    ws = web.WebSocketResponse(max_msg_size=8 * 1024 * 1024)
    await ws.prepare(request)
    identity = None
    expiry_task = None
    if role == "agent":
        authenticated = await authenticate_agent(request, ws, room)
        if authenticated is None:
            return ws
        identity, password_hash = authenticated
        state = rooms.get(room)
        if state and state.get("agent") and not state["agent"].closed:
            await ws.close(code=1013, message=b"agent already connected")
            return ws
        if state is None:
            state = {
                "agent": ws,
                "clients": set(),
                "client": None,
                "password_hash": password_hash,
            }
            rooms[room] = state
        else:
            state["agent"] = ws
            state["password_hash"] = password_hash
            for old in list(state["clients"]):
                if not old.closed:
                    await old.close(code=1012, message=b"agent reconnected")
            state["clients"].clear()
            state["client"] = None
        log.info("agent connected room=%s key=%s", room, identity.fingerprint)
    else:
        if (
            _room_state(room) is not state
            or state["password_hash"] != password_hash
        ):
            await ws.close(code=1013, message=b"room changed during connection")
            return ws
        old = state.get("client")
        state["client"] = None
        if old:
            state["clients"].discard(old)
            if not old.closed:
                await old.close(code=1012, message=b"replaced")
            await _tell_agent_client_offline(state)
        state["clients"].add(ws)
        state["client"] = ws
        log.info("client connected room=%s", room)
        await ws.send_json({"type": "agent_online"})
        if session_expires is not None:
            expiry_task = asyncio.create_task(
                _expire_client_session(ws, session_expires)
            )

    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except ValueError:
                continue
            target = state["agent"] if role == "client" else state.get("client")
            if target and not target.closed:
                await target.send_json(data)
    finally:
        if expiry_task:
            expiry_task.cancel()
        if role == "agent" and state.get("agent") is ws:
            state["agent"] = None
            for browser in list(state["clients"]):
                if not browser.closed:
                    await browser.close(code=1012, message=b"agent disconnected")
        was_active_client = role == "client" and state.get("client") is ws
        if was_active_client:
            state["client"] = None
            await _tell_agent_client_offline(state)
        state["clients"].discard(ws)
        if not state.get("agent") and not state["clients"] and rooms.get(room) is state:
            rooms.pop(room)
        log.info("%s disconnected room=%s", role, room)
    return ws


def _positive_int(config, name, default, *, minimum=1, maximum=None):
    try:
        value = int(config.get(name, default))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"browser_auth.{name} must be an integer") from exc
    if value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"browser_auth.{name} is outside the allowed range")
    return value


def create_ssl_context(tls):
    """Build the HTTPS context, or return None when both TLS paths are empty."""
    cert = tls.get("cert", "")
    key = tls.get("key", "")
    if not isinstance(cert, str) or not isinstance(key, str):
        raise TypeError("tls.cert and tls.key must be strings")
    if not cert and not key:
        return None
    if not cert or not key:
        raise ValueError("tls.cert and tls.key must both be empty or both be set")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    return context


def create_app(cfg):
    auth = cfg["auth"]
    browser_auth = cfg.get("browser_auth", {})
    default_room = cfg.get("server", {}).get("room", "home")
    validate_room_name(default_room)
    app = web.Application(client_max_size=64 * 1024)
    app[AUTHORIZED_KEYS] = load_authorized_keys(auth["public_keys"])
    app[AUTH_TIMEOUT] = float(auth.get("timeout", 10))
    app[DEFAULT_ROOM] = default_room
    app[SESSION_SECRET] = os.urandom(32)
    app[SESSION_TTL] = _positive_int(
        browser_auth,
        "session_ttl",
        12 * 60 * 60,
        minimum=60,
        maximum=30 * 24 * 60 * 60,
    )
    attempts = _positive_int(browser_auth, "max_attempts", 5, maximum=100)
    window = _positive_int(browser_auth, "attempt_window", 60, maximum=24 * 60 * 60)
    workers = _positive_int(browser_auth, "password_workers", 2, maximum=16)
    app[LOGIN_LIMITER] = LoginLimiter(attempts, window)
    app[PASSWORD_WORKERS] = asyncio.Semaphore(workers)
    app.router.add_get("/signal", signal)
    app.router.add_get("/server-healthz", health)
    app.router.add_post("/_bifrost/login", login)
    app.router.add_get("/", portal)
    app.router.add_get("/{tail:.*}", page)
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    cfg = load_config(args.config)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app = create_app(cfg)
    context = create_ssl_context(cfg.get("tls", {}))
    web.run_app(
        app,
        host=cfg["server"]["bind"],
        port=cfg["server"]["port"],
        ssl_context=context,
    )


if __name__ == "__main__":
    main()
