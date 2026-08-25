import asyncio
import logging

import aiohttp
import pytest

import bifrost.agent as agent
from bifrost.agent import rtc_configuration, select_stun_urls


def test_client_stun_urls_take_precedence_over_server_urls():
    configured = ["stun:client.example:3478"]
    server = ["stun:server.example:3478"]

    assert select_stun_urls(configured, server) == configured
    assert select_stun_urls([], server) == server


def test_rtc_configuration_never_uses_implicit_aiortc_defaults():
    empty = rtc_configuration([])
    configured = rtc_configuration([
        "stun:stun.miwifi.com:3478",
        "stun:stun.cloudflare.com:3478",
    ])

    assert empty.iceServers == []
    assert len(configured.iceServers) == 1
    assert configured.iceServers[0].urls == [
        "stun:stun.miwifi.com:3478",
        "stun:stun.cloudflare.com:3478",
    ]


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
