from importlib.resources import files

import bifrost


def test_version():
    assert bifrost.__version__ == "0.1.0"


def test_static_page_is_packaged():
    page = files("bifrost").joinpath("static/index.html")
    assert "Bifrost" in page.read_text(encoding="utf-8")
