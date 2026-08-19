from aiohttp import web
async def root(r): return web.Response(text='<h1>纯内网 HTTP</h1><a href="/healthz">healthz</a>',content_type='text/html')
async def healthz(r): return web.Response(text='ok\n')
app=web.Application(); app.router.add_get('/',root); app.router.add_get('/healthz',healthz); web.run_app(app,host='127.0.0.1',port=18080)
