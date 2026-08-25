import asyncio
import base64

from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bifrost.auth import auth_payload, public_key_text
from bifrost.server import create_app, rooms


async def authenticate_agent(client, private_key, room):
    agent = await client.ws_connect(f"/signal?role=agent&room={room}")
    challenge_message = await agent.receive_json()
    challenge = base64.b64decode(challenge_message["challenge"])
    signature = private_key.sign(auth_payload(room, challenge))
    await agent.send_json(
        {
            "type": "auth_response",
            "public_key": public_key_text(private_key.public_key()),
            "signature": base64.b64encode(signature).decode("ascii"),
        }
    )
    assert await agent.receive_json() == {"type": "auth_ok"}
    return agent


def test_agent_registers_room_before_browser_can_join():
    async def scenario():
        rooms.clear()
        private_key = Ed25519PrivateKey.generate()
        app = create_app(
            {
                "auth": {
                    "public_keys": [public_key_text(private_key.public_key())],
                    "timeout": 1,
                }
            }
        )
        client = TestClient(TestServer(app))
        await client.start_server()

        try:
            missing_room = await client.get("/signal?role=client")
            assert missing_room.status == 400
            assert await missing_room.text() == "missing room"

            unregistered_room = await client.get("/signal?role=client&room=home")
            assert unregistered_room.status == 409
            assert await unregistered_room.text() == "room not ready"

            agent = await authenticate_agent(client, private_key, "home")
            browser = await client.ws_connect("/signal?role=client&room=home")

            assert await browser.receive_json() == {"type": "agent_online"}
            await browser.send_json({"type": "offer", "sdp": {"type": "offer"}})
            assert await agent.receive_json() == {
                "type": "offer",
                "sdp": {"type": "offer"},
            }

            await browser.close()
            await agent.close()
        finally:
            await client.close()

        assert rooms == {}

    asyncio.run(scenario())
