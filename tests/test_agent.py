import asyncio
import logging
import socket

import aiohttp
import pytest
from aiortc import RTCConfiguration, RTCPeerConnection

import bifrost.agent as agent


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
