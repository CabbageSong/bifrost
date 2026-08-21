import asyncio
import base64
import json

import aiohttp
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bifrost.auth import (
    auth_payload,
    load_authorized_keys,
    load_private_key,
    public_key_bytes,
    public_key_text,
    verify_signature,
)
from bifrost.server import create_app, rooms


def write_private_key(path, key):
    path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))


def test_load_private_key_and_authorized_keys(tmp_path):
    private_key = Ed25519PrivateKey.generate()
    private_path = tmp_path / 'agent.key'
    authorized_path = tmp_path / 'authorized_keys'
    write_private_key(private_path, private_key)
    authorized_path.write_text(
        '# agents\n'
        f'room="home" {public_key_text(private_key.public_key())} test-agent\n',
        encoding='utf-8',
    )

    loaded_private = load_private_key(private_path)
    authorized = load_authorized_keys(authorized_path)
    entry = authorized[public_key_bytes(loaded_private.public_key())]
    challenge = b'x' * 32
    signature = loaded_private.sign(auth_payload('home', challenge))

    assert entry.permits('home')
    assert not entry.permits('office')
    assert verify_signature(entry, 'home', challenge, signature)
    assert not verify_signature(entry, 'office', challenge, signature)


def test_agent_websocket_authentication(tmp_path):
    async def scenario():
        rooms.clear()
        private_key = Ed25519PrivateKey.generate()
        authorized_path = tmp_path / 'authorized_keys'
        authorized_path.write_text(
            f'room="home" {public_key_text(private_key.public_key())}\n',
            encoding='utf-8',
        )
        app = create_app({'auth': {'public_keys': [authorized_path.read_text(encoding='utf-8')], 'timeout': 1}})
        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect('/signal?role=agent&room=home')
            challenge_message = await ws.receive_json()
            challenge = base64.b64decode(challenge_message['challenge'])
            signature = private_key.sign(auth_payload('home', challenge))
            await ws.send_json({
                'type': 'auth_response',
                'public_key': public_key_text(private_key.public_key()),
                'signature': base64.b64encode(signature).decode('ascii'),
            })
            assert await ws.receive_json() == {'type': 'auth_ok'}
            assert rooms['home']['agent'] is not None
            await ws.close()
        rooms.clear()

    asyncio.run(scenario())


def test_agent_key_cannot_access_unauthorized_room(tmp_path):
    async def scenario():
        rooms.clear()
        private_key = Ed25519PrivateKey.generate()
        authorized_path = tmp_path / 'authorized_keys'
        authorized_path.write_text(
            f'room="home" {public_key_text(private_key.public_key())}\n',
            encoding='utf-8',
        )
        app = create_app({'auth': {'public_keys': [authorized_path.read_text(encoding='utf-8')], 'timeout': 1}})
        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect('/signal?role=agent&room=office')
            challenge_message = await ws.receive_json()
            challenge = base64.b64decode(challenge_message['challenge'])
            signature = private_key.sign(auth_payload('office', challenge))
            await ws.send_json({
                'type': 'auth_response',
                'public_key': public_key_text(private_key.public_key()),
                'signature': base64.b64encode(signature).decode('ascii'),
            })
            assert await ws.receive_json() == {
                'type': 'auth_error',
                'error': 'authentication failed',
            }
            close = await ws.receive()
            assert close.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED)
            assert 'office' not in rooms
        rooms.clear()

    asyncio.run(scenario())


def test_inline_public_keys_accept_pub_format(tmp_path):
    key = Ed25519PrivateKey.generate()
    pub_line = public_key_text(key.public_key()) + ' laptop-comment'
    restricted = 'room="home" ' + pub_line
    entries = load_authorized_keys([pub_line, restricted])
    assert len(entries) == 1
    entry = entries[public_key_bytes(key.public_key())]
    # An unrestricted duplicate remains unrestricted, as with authorized_keys.
    assert entry.permits('home')
    assert entry.permits('office')
