from importlib.resources import files

import bifrost


def test_version():
    assert bifrost.__version__ == "0.1.0"


def test_static_page_is_packaged():
    page = files("bifrost").joinpath("static/index.html")
    text = page.read_text(encoding="utf-8")
    assert "Bifrost" in text
    assert "bifrostConfig.iceServers" in text
    assert "bifrostConfig.stunUrls" in text
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
    assert "window.XMLHttpRequest=ProxyXHR" in text
    assert "window.addEventListener('submit'" in text
    assert "if(!form||e.defaultPrevented)return" in text
    assert "document.addEventListener('submit'" not in text
    assert "navigator.sendBeacon=function" in text
    assert "body_base64" in text
    assert "formmethod" in text
    assert "response_url" in text
    assert "allow-scripts allow-forms'" in text
    assert "allow-same-origin" not in text


def test_static_page_logs_ice_diagnostics_without_turn_secrets():
    text = files("bifrost").joinpath("static/index.html").read_text(encoding="utf-8")
    assert "onicecandidateerror" in text
    assert "onicegatheringstatechange" in text
    assert "remote answer candidates" in text
    assert "candidate-pair" in text
    assert "pc.getStats()" in text
    assert "unhandledrejection" in text
    assert "credential:server.credential?'configured':undefined" in text
