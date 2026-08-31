from importlib.resources import files

import bifrost


def test_version():
    assert bifrost.__version__ == "0.1.0"


def test_static_page_is_packaged():
    page = files("bifrost").joinpath("static/index.html")
    text = page.read_text(encoding="utf-8")
    assert "Bifrost" in text
    assert "bifrostConfig.iceServers" in text
    assert "stun.l.google.com" not in text


def test_room_entry_and_login_pages_are_packaged():
    static = files("bifrost").joinpath("static")
    assert "进入 Bifrost Room" in static.joinpath("portal.html").read_text(
        encoding="utf-8"
    )
    assert "需要访问密码" in static.joinpath("login.html").read_text(
        encoding="utf-8"
    )


def test_static_page_bridges_http_methods_and_browser_apis():
    text = files("bifrost").joinpath("static/index.html").read_text(encoding="utf-8")
    assert "method:method||'GET'" in text
    assert "window.fetch=function" in text
    assert "window.addEventListener('submit'" in text
    assert "if(!form||e.defaultPrevented)return" in text
    assert "document.addEventListener('submit'" not in text
    assert "body_base64" in text
    assert "formmethod" in text
    assert "response_url" in text
    assert "allow-scripts allow-forms'" in text
    assert "allow-same-origin" not in text


def test_static_page_bridges_subresources_navigation_and_websockets():
    text = files("bifrost").joinpath("static/index.html").read_text(encoding="utf-8")
    assert "async function prepareHtml" in text
    assert "script[src]" in text
    assert "img[src]" in text
    assert "'data:'+type.split(';',1)[0]+';base64,'+body" in text
    assert "URL.createObjectURL" not in text
    assert "doc.head.insertAdjacentHTML('afterbegin',frameHook(currentPath))" in text
    assert "f.srcdoc=prepared" in text
    assert "window.WebSocket=ProxyWebSocket" in text
    assert "type:'websocket_open'" in text
    assert "type:'websocket_send'" in text
    assert "type:'websocket_close'" in text
    assert "window.__bifrostNavigate" in text
    assert "location\\s*\\." in text


def test_static_page_logs_ice_diagnostics_without_turn_secrets():
    text = files("bifrost").joinpath("static/index.html").read_text(encoding="utf-8")
    assert "onicecandidateerror" in text
    assert "onicegatheringstatechange" in text
    assert "remote answer candidates" in text
    assert "candidate-pair" in text
    assert "pc.getStats()" in text
    assert "unhandledrejection" in text
    assert "credential:server.credential?'configured':undefined" in text


def test_static_page_reconnects_web_rtc_transport():
    text = files("bifrost").joinpath("static/index.html").read_text(encoding="utf-8")
    assert "function scheduleReconnect" in text
    assert "function reconnectNow" in text
    assert "function teardownTransport" in text
    assert "ICE disconnected for 5 seconds" in text
    assert "Math.min(1000*2**Math.min(reconnectAttempt,4),16000)" in text
    assert "window.addEventListener('online'" in text
    assert "document.addEventListener('visibilitychange'" in text
    assert "$('refresh').onclick=()=>{if(dc&&dc.readyState==='open')" in text
    assert "bifrostConfig.protected&&event.code===1008" in text
