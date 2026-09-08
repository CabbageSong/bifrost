import asyncio
import logging
import socket

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import bifrost.agent as agent
from bifrost.auth import public_key_text
from bifrost.server import create_app, rooms


def test_dispatch_uses_the_bidirectional_handler_contract():
    async def scenario():
        sent = []

        async def reply(message):
            sent.append(message)

        class Handler:
            async def __call__(self, message, send):
                await send({"type": "reply", "id": message["id"]})

        await agent._dispatch('{"id":1}', Handler(), reply)
        assert sent == [{"type": "reply", "id": 1}]

    asyncio.run(scenario())


def test_dispatch_unpacks_binary_packets():
    async def scenario():
        sent = []

        async def reply(message):
            sent.append(message)

        class Handler:
            async def __call__(self, message, send):
                await send({
                    "type": "reply",
                    "id": message["id"],
                    "body": message["body"] + b":pong",
                })

        from bifrost.protocol import encode_packet

        packet = encode_packet({"type": "ping", "id": 42}, b"ping")
        await agent._dispatch(packet, Handler(), reply)
        assert sent == [{"type": "reply", "id": 42, "body": b"ping:pong"}]

    asyncio.run(scenario())


def test_dispatch_unpacks_legacy_base64_http_request():
    async def scenario():
        sent = []

        async def reply(message):
            sent.append(message)

        class Handler:
            async def __call__(self, message, send):
                await send({
                    "type": "reply",
                    "id": message["id"],
                    "body": message["body"],
                })

        raw = '{"type": "http_request", "id": 101, "method": "POST", "path": "/", "headers": {}, "body_base64": "aGVsbG8="}'
        await agent._dispatch(raw, Handler(), reply)
        assert sent == [{"type": "reply", "id": 101, "body": b"hello"}]

    asyncio.run(scenario())


def test_dispatch_unpacks_legacy_base64_websocket_binary_message():
    async def scenario():
        sent = []

        async def reply(message):
            sent.append(message)

        class Handler:
            async def __call__(self, message, send):
                await send({
                    "type": "reply",
                    "socket_id": message["socket_id"],
                    "body": message["body"],
                })

        raw = '{"type": "websocket_send", "socket_id": "sock-1", "data_type": "binary", "data": "d29ybGQ="}'
        await agent._dispatch(raw, Handler(), reply)
        assert sent == [{"type": "reply", "socket_id": "sock-1", "body": b"world"}]

    asyncio.run(scenario())


def test_dispatch_maps_websocket_failures_to_websocket_events():
    async def scenario():
        sent = []

        async def reply(message):
            sent.append(message)

        class Handler:
            async def __call__(self, _message, _send):
                raise ValueError("invalid websocket path")

        await agent._dispatch(
            '{"type":"websocket_open","socket_id":"socket-1"}',
            Handler(),
            reply,
        )
        assert sent == [{
            "type": "websocket_open_failed",
            "socket_id": "socket-1",
            "error": "invalid websocket path",
        }]

    asyncio.run(scenario())


def test_bind_ice_port_is_task_local():
    async def scenario():
        probes = []
        ports = []
        for _ in range(2):
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.bind(("127.0.0.1", 0))
            probes.append(probe)
            ports.append(probe.getsockname()[1])
        for probe in probes:
            probe.close()

        async def bind(port):
            with agent.bind_ice_port(port):
                transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                    asyncio.DatagramProtocol,
                    local_addr=("127.0.0.1", 0),
                )
            return transport

        transports = await asyncio.gather(*(bind(port) for port in ports))
        try:
            assert [
                transport.get_extra_info("sockname")[1] for transport in transports
            ] == ports
        finally:
            for transport in transports:
                transport.close()

    asyncio.run(scenario())


def test_bind_ice_port_allows_multiple_peers_to_share_a_room_port():
    async def scenario():
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()

        async def bind():
            with agent.bind_ice_port(port):
                return await asyncio.get_running_loop().create_datagram_endpoint(
                    asyncio.DatagramProtocol,
                    local_addr=("127.0.0.1", 0),
                )

        first, second = await asyncio.gather(bind(), bind())
        try:
            assert first[0].get_extra_info("sockname")[1] == port
            assert second[0].get_extra_info("sockname")[1] == port
        finally:
            first[0].close()
            second[0].close()

    asyncio.run(scenario())


def test_answer_uses_fixed_ice_host_candidate_port():
    async def scenario():
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("0.0.0.0", 0))
        port = probe.getsockname()[1]
        probe.close()

        browser = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        local_agent = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        browser.createDataChannel("http")
        try:
            await browser.setLocalDescription(await browser.createOffer())
            await local_agent.setRemoteDescription(browser.localDescription)
            with agent.bind_ice_port(port):
                await local_agent.setLocalDescription(await local_agent.createAnswer())
            candidates = (
                local_agent.sctp.transport.transport.iceGatherer.getLocalCandidates()
            )
            assert {
                candidate.port for candidate in candidates if candidate.type == "host"
            } == {port}
        finally:
            await browser.close()
            await local_agent.close()

    asyncio.run(scenario())


def test_agent_handles_two_browser_peers_on_one_room_port():
    async def scenario():
        rooms.clear()
        private_key = Ed25519PrivateKey.generate()
        cfg = {
            "server": {"bind": "127.0.0.1", "port": 8443},
            "webrtc": {"ice_servers": []},
            "tls": {"cert": "", "key": ""},
            "auth": {
                "public_keys": [public_key_text(private_key.public_key())],
                "timeout": 2,
            },
            "browser_auth": {
                "session_ttl": 300,
                "max_attempts": 5,
                "attempt_window": 60,
                "password_workers": 1,
            },
        }

        class Handler:
            async def __call__(self, _message, _reply):
                pass

            async def close_connection(self, _reply):
                pass

        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        ice_port = probe.getsockname()[1]
        probe.close()

        async with TestClient(TestServer(create_app(cfg))) as client:
            agent_cfg = {
                "signal": {
                    "url": str(client.make_url("/signal")),
                    "room": "home",
                    "verify_tls": True,
                },
                "auth": {"timeout": 2},
                "browser_auth": {"password_hash": ""},
                "webrtc": {"ice_servers": [], "ice_port": ice_port},
            }
            agent_task = asyncio.create_task(
                agent.run_agent(
                    agent_cfg,
                    Handler(),
                    identity=(private_key, private_key.public_key()),
                )
            )
            browsers = []
            try:
                for _ in range(100):
                    if rooms.get("home", {}).get("agent"):
                        break
                    await asyncio.sleep(0.01)
                assert rooms.get("home", {}).get("agent")

                opened = []
                for _ in range(2):
                    websocket = await client.ws_connect(
                        "/signal?role=client&room=home"
                    )
                    assert await websocket.receive_json() == {"type": "agent_online"}
                    peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
                    channel = peer.createDataChannel("http")
                    channel_opened = asyncio.Event()
                    channel.on("open", channel_opened.set)
                    opened.append(channel_opened)
                    await peer.setLocalDescription(await peer.createOffer())
                    await websocket.send_json({
                        "type": "offer",
                        "sdp": {
                            "type": peer.localDescription.type,
                            "sdp": peer.localDescription.sdp,
                        },
                    })
                    while True:
                        message = await websocket.receive_json()
                        if message["type"] == "answer":
                            await peer.setRemoteDescription(
                                RTCSessionDescription(**message["sdp"])
                            )
                            break
                    browsers.append((websocket, peer))

                await asyncio.wait_for(
                    asyncio.gather(*(event.wait() for event in opened)),
                    timeout=10,
                )
                assert len(rooms["home"]["clients"]) == 2
            finally:
                for websocket, peer in browsers:
                    await websocket.close()
                    await peer.close()
                if not agent_task.done():
                    agent_task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await agent_task
        rooms.clear()

    for _ in range(5):
        try:
            asyncio.run(scenario())
            return
        except (TimeoutError, asyncio.TimeoutError):
            pass
    asyncio.run(scenario())


def test_empty_client_ice_list_uses_direct_configuration(monkeypatch):
    captured = {}

    class Signal:
        closed = False

        def __aiter__(self):
            async def messages():
                if False:
                    yield None

            return messages()

    class Connection:
        async def __aenter__(self):
            return Signal()

        async def __aexit__(self, *args):
            pass

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def ws_connect(self, *args, **kwargs):
            return Connection()

    async def authenticate(*args, **kwargs):
        return None

    def rtc_configuration(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(agent.aiohttp, "ClientSession", Session)
    monkeypatch.setattr(agent, "authenticate", authenticate)
    monkeypatch.setattr(agent, "RTCConfiguration", rtc_configuration)

    cfg = {
        "signal": {
            "url": "wss://example.test",
            "room": "home",
            "verify_tls": True,
        },
        "auth": {"timeout": 10},
        "browser_auth": {"password_hash": ""},
        "webrtc": {"ice_servers": [], "ice_port": 37665},
    }
    asyncio.run(agent.run_agent(cfg, None, identity=(object(), object())))

    assert captured["iceServers"] == []


def test_serve_agent_backs_off_connection_failures_without_tracebacks(
    monkeypatch, caplog
):
    delays = []

    async def fail_to_connect(*args):
        raise aiohttp.ClientConnectionError("connection refused")

    async def sleep(delay):
        delays.append(delay)
        if len(delays) == 4:
            raise asyncio.CancelledError

    monkeypatch.setattr(agent, "run_agent", fail_to_connect)
    monkeypatch.setattr(agent.asyncio, "sleep", sleep)
    cfg = {"signal": {"room": "home"}}

    with caplog.at_level(logging.WARNING, logger="bifrost.agent"):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(
                agent.serve_agent(
                    cfg,
                    None,
                    reconnect_delay=2,
                    max_reconnect_delay=5,
                    identity=object(),
                )
            )

    assert delays == [2, 4, 5, 5]
    assert len(caplog.records) == 4
    assert all(record.exc_info is None for record in caplog.records)
    assert all("room=home" in record.getMessage() for record in caplog.records)


def test_serve_agent_keeps_tracebacks_for_unexpected_failures(monkeypatch, caplog):
    async def fail(*args):
        raise RuntimeError("unexpected")

    async def stop(_delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(agent, "run_agent", fail)
    monkeypatch.setattr(agent.asyncio, "sleep", stop)
    cfg = {"signal": {"room": "home"}}

    with caplog.at_level(logging.ERROR, logger="bifrost.agent"):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(agent.serve_agent(cfg, None, identity=object()))

    assert len(caplog.records) == 1
    assert caplog.records[0].exc_info is not None


def test_serve_agent_resets_backoff_after_a_completed_session(monkeypatch):
    outcomes = iter([aiohttp.ClientConnectionError("offline"), None])
    delays = []

    async def run(*args):
        outcome = next(outcomes)
        if outcome:
            raise outcome

    async def sleep(delay):
        delays.append(delay)
        if len(delays) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(agent, "run_agent", run)
    monkeypatch.setattr(agent.asyncio, "sleep", sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            agent.serve_agent(
                {"signal": {"room": "home"}},
                None,
                reconnect_delay=2,
                identity=object(),
            )
        )

    assert delays == [2, 2]
