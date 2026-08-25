import argparse, asyncio, json, logging
import aiohttp
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.sdp import candidate_from_sdp
from .protocol import decode_body, encode_body, load_config, http_response

log = logging.getLogger("bifrost.client")


async def run(cfg):
    signal = cfg["signal"]
    target = cfg["local_http"]["target"]
    # One session keeps upstream cookies and pooled connections for the life of
    # the peer. unsafe=True is needed for the common 127.0.0.1 target.
    cookie_jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(cookie_jar=cookie_jar) as session:
        async with session.ws_connect(
            signal["url"],
            params={"role": "agent", "room": signal["room"]},
            ssl=False if not signal.get("verify_tls", True) else None,
            heartbeat=20,
            max_msg_size=8 * 1024 * 1024,
        ) as sig:
            pc = None
            pending = []

            async def send(x):
                if not sig.closed:
                    await sig.send_json(x)

            async def new_pc():
                nonlocal pc
                if pc:
                    await pc.close()
                pc = RTCPeerConnection()

                @pc.on("icecandidate")
                async def ice(c):
                    if c:
                        await send(
                            {
                                "type": "candidate",
                                "candidate": {
                                    "candidate": c.to_sdp(),
                                    "sdpMid": c.sdpMid,
                                    "sdpMLineIndex": c.sdpMLineIndex,
                                },
                            }
                        )

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
                        asyncio.create_task(handle_http(ch, raw, target, session))

                return pc

            async for msg in sig:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                m = json.loads(msg.data)
                if m.get("type") == "offer":
                    pc = await new_pc()
                    await pc.setRemoteDescription(RTCSessionDescription(**m["sdp"]))
                    for c in pending:
                        try:
                            await pc.addIceCandidate(c)
                        except Exception:
                            pass
                    pending = []
                    answer = await pc.createAnswer()
                    await pc.setLocalDescription(answer)
                    await send(
                        {
                            "type": "answer",
                            "sdp": {
                                "sdp": pc.localDescription.sdp,
                                "type": pc.localDescription.type,
                            },
                        }
                    )
                elif m.get("type") == "candidate" and m.get("candidate"):
                    c = m["candidate"]
                    cand = candidate_from_sdp(c["candidate"])
                    cand.sdpMid = c.get("sdpMid")
                    cand.sdpMLineIndex = c.get("sdpMLineIndex")
                    if pc and pc.remoteDescription:
                        await pc.addIceCandidate(cand)
                    else:
                        pending.append(cand)
            if pc:
                await pc.close()


async def handle_http(ch, raw, target, session):
    try:
        m = json.loads(raw)
        rid = m.get("id")
        if m.get("type") != "http_request":
            return
        path = m.get("path") or "/"
        path = path if path.startswith("/") else "/" + path
        headers = {
            k: v
            for k, v in m.get("headers", {}).items()
            if k.lower() not in ("host", "content-length")
        }
        if m.get("body_base64") is not None:
            body = decode_body(m["body_base64"])
        else:
            body = (m.get("body") or "").encode("utf-8")
        method = (m.get("method") or "GET").upper()
        async with session.request(
            method,
            target.rstrip("/") + path,
            headers=headers,
            data=body or None,
        ) as r:
            response_body = await r.read()
            result = http_response(
                rid,
                r.status,
                dict(r.headers),
                response_body.decode("utf-8", "replace"),
                body_base64=encode_body(response_body),
                status_text=r.reason or "",
                response_url=r.url.raw_path_qs,
            )
        ch.send(json.dumps(result))
    except Exception as e:
        ch.send(
            json.dumps(
                http_response(rid if "rid" in locals() else None, 502, error=str(e))
            )
        )


async def main(cfg):
    while True:
        try:
            await run(cfg)
        except Exception:
            log.exception("client loop stopped")
        await asyncio.sleep(2)


def cli():
    ap = argparse.ArgumentParser(description="Run the Bifrost private HTTP client")
    ap.add_argument(
        "--config", required=True, help="path to the TOML configuration file"
    )
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    asyncio.run(main(load_config(args.config)))


if __name__ == "__main__":
    cli()
