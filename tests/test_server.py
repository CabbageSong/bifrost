import asyncio

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from bifrost.server import rooms, signal


def test_agent_registers_room_before_browser_can_join():
    async def scenario():
        rooms.clear()
        app = web.Application()
        app.router.add_get("/signal", signal)
        client = TestClient(TestServer(app))
        await client.start_server()

        try:
            missing_room = await client.get("/signal?role=client")
            assert missing_room.status == 400
            assert await missing_room.text() == "missing room"

            unregistered_room = await client.get("/signal?role=client&room=home")
            assert unregistered_room.status == 409
            assert await unregistered_room.text() == "room not ready"

            agent = await client.ws_connect("/signal?role=agent&room=home")
            browser = await client.ws_connect("/signal?role=client&room=home")

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
