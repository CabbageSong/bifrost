import argparse, asyncio, json, logging, ssl
from importlib.resources import files
from aiohttp import web, WSMsgType
from .protocol import load_config

log=logging.getLogger('bifrost.server'); rooms={}

SHELL=files('bifrost').joinpath('static/index.html').read_text(encoding='utf-8')

async def page(request):
    return web.Response(text=SHELL,content_type='text/html',headers={'Cache-Control':'no-store'})
async def health(request): return web.Response(text='ok\n')
async def signal(request):
    role=request.query.get('role'); room=request.query.get('room','').strip()
    if role not in ('client','agent'): return web.Response(status=400,text='bad role')
    if not room: return web.Response(status=400,text='missing room')
    if role=='agent':
        state=rooms.setdefault(room,{'agent':None,'clients':set()})
    else:
        state=rooms.get(room)
        if not state or not state['agent'] or state['agent'].closed:
            return web.Response(status=409,text='room not ready')
    ws=web.WebSocketResponse(max_msg_size=8*1024*1024); await ws.prepare(request)
    if role=='agent':
        if state['agent'] and not state['agent'].closed:
            await ws.close(code=1013,message=b'agent already connected'); return ws
        state['agent']=ws; log.info('agent connected room=%s',room)
    else:
        for old in list(state['clients']):
            if not old.closed: await old.close(code=1012,message=b'replaced')
        state['clients'].clear(); state['clients'].add(ws); log.info('client connected room=%s',room)
        if state['agent'] and not state['agent'].closed: await ws.send_json({'type':'agent_online'})
    try:
        async for msg in ws:
            if msg.type==WSMsgType.TEXT:
                try: data=json.loads(msg.data)
                except ValueError: continue
                target=state['agent'] if role=='client' else next((x for x in state['clients'] if not x.closed),None)
                if target and not target.closed: await target.send_json(data)
    finally:
        if role=='agent' and state.get('agent') is ws: state['agent']=None
        state['clients'].discard(ws)
        if not state['agent'] and not state['clients'] and rooms.get(room) is state: rooms.pop(room)
        log.info('%s disconnected room=%s',role,room)
    return ws

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--config',required=True); args=ap.parse_args(); cfg=load_config(args.config)
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    app=web.Application(); app.router.add_get('/signal',signal); app.router.add_get('/server-healthz',health); app.router.add_get('/',page); app.router.add_get('/{tail:.*}',page)
    ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(cfg['tls']['cert'],cfg['tls']['key'])
    web.run_app(app,host=cfg['server']['bind'],port=cfg['server']['port'],ssl_context=ctx)
if __name__=='__main__': main()
