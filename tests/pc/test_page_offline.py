"""The settings page loads nothing from the network.

It is served by the daemon to a desk that may be offline, from inside a
frozen app, under a Content-Security-Policy that allows this origin only.
The one thing it may name outside its own text is the two vendored
Montserrat files, read through /font/.
"""
import re

from pc import theme_server

FONT = re.compile(r"^/font/Montserrat-[A-Za-z]+-latin\.woff2$")


def _page():
    return theme_server.page()


def _urls(page):
    css = re.sub(r"/\*.*?\*/", "", page[page.index("<style>"):
                                        page.index("</style>")], flags=re.S)
    html = re.sub(r"<!--.*?-->", "", page, flags=re.S)
    found = re.findall(r"""<link[^>]*\shref=["']([^"']+)""", html)
    found += re.findall(r"""<script[^>]*\ssrc=["']([^"']+)""", html)
    found += re.findall(r"""@import\s+(?:url\()?["']?([^"')\s;]+)""", css)
    found += re.findall(r"""url\(\s*["']?([^"')]+)""", css)
    return found


def test_the_page_names_no_outside_url_but_its_two_fonts():
    urls = _urls(_page())
    assert urls, "the @font-face urls should have been found"
    for u in urls:
        assert u.startswith("#") or FONT.match(u), u
    fonts = [u for u in urls if FONT.match(u)]
    assert sorted(set(fonts)) == sorted(
        "/font/" + n for n in theme_server.FONT_NAMES)


def test_no_scheme_or_protocol_relative_url_appears_in_markup_or_style():
    page = _page()
    head = page[:page.index("<script>")]
    rest = head.replace("http://www.w3.org/2000/svg", "")
    assert not re.search(r"https?://", rest)
    assert not re.search(r"""(?:src|href)=["']//""", rest)
    assert not re.search(r"""url\(\s*["']?//""", rest)


def test_the_csp_still_forbids_everything_else(tmp_path):
    import http.client
    import urllib.parse
    from tests.pc.test_theme_server import login, request
    s = theme_server.BuilderServer(str(tmp_path / ".blink"), tick_s=3600)
    s.start()
    try:
        cookie = login(s)
        _, headers, _ = request(s, "GET", "/", cookie=cookie)
        csp = headers["Content-Security-Policy"]
        assert "default-src 'none'" in csp and "font-src 'self'" in csp
        assert "img-src data:" in csp and "frame-ancestors 'none'" in csp
        # font-src is the only source added; no other directive widens.
        for d in csp.split(";"):
            name = d.split()[0]
            if name != "font-src":
                assert "*" not in d and "http" not in d, d
    finally:
        s.finish("test over")
        s.wait(5)
