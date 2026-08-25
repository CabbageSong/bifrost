from importlib.resources import files

import bifrost


def test_version():
    assert bifrost.__version__ == "0.1.0"


def test_static_page_is_packaged():
    page = files("bifrost").joinpath("static/index.html")
    text = page.read_text(encoding="utf-8")
    assert "Bifrost" in text


def test_static_page_bridges_http_methods_and_browser_apis():
    text = files("bifrost").joinpath("static/index.html").read_text(encoding="utf-8")
    assert "method:method||'GET'" in text
    assert "window.fetch=function" in text
    assert "window.XMLHttpRequest=ProxyXHR" in text
    assert "document.addEventListener('submit'" in text
    assert "navigator.sendBeacon=function" in text
    assert "body_base64" in text
    assert "formmethod" in text
    assert "response_url" in text
