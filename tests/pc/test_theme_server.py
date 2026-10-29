"""The theme builder's local server, and the code that opens a browser on it.

Real sockets, on loopback only (tests/conftest.py refuses anything else).
No browser is ever started: every opener is a stub.
"""
import http.client
import json
import os
import re
import socket
import threading
import urllib.parse

import pytest

from pc import theme
from pc import theme_server


DARKISH = {"id": 12,
           "c": ["0e1116", "e6e8eb", "4387df", "0da243",
                 "0da243", "ba8107", "ff1900"],
           "w": 12, "ends": "round", "dot": 1, "by": "level"}


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


@pytest.fixture
def home(tmp_path):
    return str(tmp_path / ".blink")


@pytest.fixture
def server(home):
    clock = Clock()
    s = theme_server.BuilderServer(home, now=clock, tick_s=3600)
    s.clock = clock
    s.start()
    yield s
    s.finish("test over")
    s.wait(5)


def request(server, method, path, body=None, cookie=None, host=None,
            origin=None, ctype="application/json"):
    c = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    headers = {"Host": host or f"127.0.0.1:{server.port}"}
    if cookie:
        headers["Cookie"] = cookie
    if origin:
        headers["Origin"] = origin
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        headers["Content-Type"] = ctype
    c.request(method, path, body=data, headers=headers)
    r = c.getresponse()
    out = r.status, dict(r.getheaders()), r.read()
    c.close()
    return out


def login(server):
    link = urllib.parse.urlsplit(server.new_link())
    status, headers, _ = request(server, "GET",
                                 link.path + "?" + link.query)
    assert status == 303
    cookie = headers["Set-Cookie"].split(";", 1)[0]
    return cookie


# ------------------------------------------------------------- binding

def test_it_binds_to_loopback_only(server):
    assert server.address == ("127.0.0.1", server.port)
    assert server.port > 0


def test_the_link_is_loopback_with_a_long_token(server):
    link = urllib.parse.urlsplit(server.new_link())
    assert link.scheme == "http"
    assert link.hostname == "127.0.0.1"
    token = urllib.parse.parse_qs(link.query)["k"][0]
    assert len(token) >= 32


def test_the_listening_socket_is_bound_to_loopback(server):
    # Not 0.0.0.0, and not "localhost", which may resolve to ::1 or worse.
    host, _ = server.listening_socket().getsockname()[:2]
    assert host == "127.0.0.1"


# ------------------------------------------------------------- tokens

@pytest.mark.parametrize("path", ["/", "/api/state", "/favicon.ico",
                                  "/open", "/open?k=nope"])
def test_nothing_is_served_without_the_token(server, path):
    status, _, body = request(server, "GET", path)
    assert status == 403
    assert b"<svg" not in body


@pytest.mark.parametrize("path", ["/api/preview", "/api/save",
                                  "/api/cancel", "/api/closed"])
def test_no_write_is_taken_without_the_token(server, home, path):
    status, _, _ = request(server, "POST", path, {"id": 1})
    assert status == 403
    assert theme.load_saved(home) is None
    assert theme.take_preview(home) is None
    assert not server.done


def test_the_link_works_once(server):
    link = urllib.parse.urlsplit(server.new_link())
    path = link.path + "?" + link.query
    assert request(server, "GET", path)[0] == 303
    assert request(server, "GET", path)[0] == 403


def test_the_cookie_is_strict_and_http_only(server):
    link = urllib.parse.urlsplit(server.new_link())
    _, headers, _ = request(server, "GET", link.path + "?" + link.query)
    c = headers["Set-Cookie"]
    assert "HttpOnly" in c and "SameSite=Strict" in c
    assert headers["Location"] == "/"


def test_the_page_is_served_with_the_session(server):
    cookie = login(server)
    status, headers, body = request(server, "GET", "/", cookie=cookie)
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert b"<svg" in body
    assert "no-store" in headers["Cache-Control"]
    assert "default-src 'none'" in headers["Content-Security-Policy"]


def test_a_made_up_cookie_is_refused(server):
    login(server)
    status, _, _ = request(server, "GET", "/", cookie="blink_theme=guess")
    assert status == 403


def test_another_host_name_is_refused_even_with_the_session(server):
    # DNS rebinding: a page on evil.example resolves its own name to
    # 127.0.0.1. The browser then sends that name as Host.
    cookie = login(server)
    status, _, _ = request(server, "GET", "/", cookie=cookie,
                           host=f"evil.example:{server.port}")
    assert status == 403


def test_a_post_from_another_origin_is_refused(server, home):
    cookie = login(server)
    status, _, _ = request(server, "POST", "/api/save", DARKISH,
                           cookie=cookie, origin="http://evil.example")
    assert status == 403
    assert theme.load_saved(home) is None


def test_the_server_never_logs_a_request(server, capsys):
    cookie = login(server)
    request(server, "GET", "/", cookie=cookie)
    err = capsys.readouterr().err
    assert "GET" not in err and "open?k=" not in err


# ------------------------------------------------------------- the API

def test_state_reports_the_saved_theme(server, home):
    theme.save(home, DARKISH)
    cookie = login(server)
    status, _, body = request(server, "GET", "/api/state", cookie=cookie)
    assert status == 200
    assert json.loads(body)["saved"] == DARKISH


def test_a_preview_reaches_the_preview_file(server, home):
    cookie = login(server)
    status, _, _ = request(server, "POST", "/api/preview", DARKISH,
                           cookie=cookie,
                           origin=f"http://127.0.0.1:{server.port}")
    assert status == 200
    assert theme.take_preview(home) == ("preview", DARKISH)
    assert not server.done


def test_a_bad_theme_is_refused_with_a_reason(server, home):
    cookie = login(server)
    status, _, body = request(server, "POST", "/api/preview",
                              {"id": 12, "c": ["ff0000"]}, cookie=cookie)
    assert status == 400
    assert json.loads(body)["error"]
    assert theme.take_preview(home) is None


def test_an_oversize_body_is_refused(server, home):
    cookie = login(server)
    status, _, _ = request(server, "POST", "/api/preview",
                           b"{" + b" " * 70000 + b"}", cookie=cookie)
    assert status == 413
    assert theme.take_preview(home) is None


def test_save_writes_the_theme_and_shuts_down(server, home):
    cookie = login(server)
    theme.write_preview(home, {"id": 3})
    status, _, _ = request(server, "POST", "/api/save", DARKISH,
                           cookie=cookie)
    assert status == 200
    assert server.wait(5) == "saved"
    assert theme.load_saved(home) == DARKISH
    # The preview still waiting is replaced by a cancel: what was saved is
    # what goes out, and a preview already shown is ended (review NICE-4).
    assert theme.take_preview(home) == ("cancel", None)


def _daemon_sends_after_save(server, home, stored, picked):
    """The lines a daemon's next tick sends after the builder saves
    `picked`, on a board holding `stored` with a preview showing."""
    theme.save(home, stored)
    sync = theme.ThemeSync(home)
    sync.board_now({"t": "theme_now", "v": 2, "id": stored["id"],
                    "why": "boot"})
    cookie = login(server)
    request(server, "POST", "/api/preview", {"id": 3}, cookie=cookie)
    assert sync.outgoing() == [theme.message("preview", {"id": 3})]
    status, _, _ = request(server, "POST", "/api/save", picked,
                           cookie=cookie)
    assert status == 200
    assert server.wait(5) == "saved"
    return sync.outgoing()


def test_saving_the_stored_theme_ends_the_preview(server, home):
    # NICE-4: no save is owed, so before this nothing went out and the
    # preview stayed on the board for 15 minutes under a page saying Saved.
    assert _daemon_sends_after_save(server, home, {"id": 4}, {"id": 4}) == [
        theme.message("cancel")]


def test_saving_a_new_theme_sends_only_the_save(server, home):
    # A cancel before the save would repaint the old stored theme for a
    # moment; the save ends the preview on the board by itself.
    assert _daemon_sends_after_save(server, home, {"id": 4}, {"id": 6}) == [
        theme.message("save", {"id": 6})]


def test_cancel_restores_and_shuts_down(server, home):
    cookie = login(server)
    request(server, "POST", "/api/preview", {"id": 3}, cookie=cookie)
    status, _, _ = request(server, "POST", "/api/cancel", b"",
                           cookie=cookie)
    assert status == 200
    assert server.wait(5) == "cancelled"
    assert theme.take_preview(home) == ("cancel", None)
    assert theme.load_saved(home) is None


def test_closing_the_tab_cancels_after_a_grace(server, home):
    cookie = login(server)
    request(server, "POST", "/api/closed", b"", cookie=cookie)
    server.tick()
    assert not server.done           # a reload says closed, then comes back
    server.clock.t += theme_server.CLOSE_GRACE_S + 1
    server.tick()
    assert server.wait(5) == "closed"
    assert theme.take_preview(home) == ("cancel", None)


def test_a_reload_inside_the_grace_keeps_the_builder_open(server):
    cookie = login(server)
    request(server, "POST", "/api/closed", b"", cookie=cookie)
    server.clock.t += 1
    request(server, "GET", "/api/state", cookie=cookie)
    server.clock.t += theme_server.CLOSE_GRACE_S + 1
    server.tick()
    assert not server.done


def test_ten_minutes_idle_cancels(server, home):
    cookie = login(server)
    server.clock.t += 9 * 60
    request(server, "POST", "/api/preview", {"id": 2}, cookie=cookie)
    server.clock.t += 9 * 60
    server.tick()
    assert not server.done           # the preview was activity
    server.clock.t += 61
    server.tick()
    assert server.wait(5) == "idle"
    assert theme.take_preview(home) == ("cancel", None)
    assert theme_server.IDLE_S == 600


def test_after_it_finishes_the_port_is_closed(server):
    port = server.port
    server.finish("cancelled")
    server.wait(5)
    with pytest.raises(OSError):
        s = socket.create_connection(("127.0.0.1", port), timeout=1)
        s.close()


def _markup_and_script(page):
    i = page.index("<script>")
    return page[:i], page[i:]


def test_every_element_the_script_looks_up_is_in_the_markup():
    """The 2026-10-29 redesign moved every control into new markup around
    an unchanged script. The script does nothing but look its elements up
    by id, and a missing one is a TypeError that stops it on load: no
    preview, no Save, a page that only looks right. So every id it names,
    and the two sliders it names through a variable, must be there."""
    markup, script = _markup_and_script(theme_server.page())
    wanted = set(re.findall(r"getElementById\('([\w-]+)'\)", script))
    wanted |= {"p1", "o1", "p2", "o2"}
    # Not an empty search: the ids the page cannot work without are in it.
    assert {"save", "cancel", "status", "status-text", "scr", "presets",
            "slots", "code", "paste", "load"} <= wanted
    for name in sorted(wanted):
        assert f'id="{name}"' in markup, name
    # The m-<slot> messages are made with their rows, but the share box's
    # is markup: fieldSay('paste', ...) writes nothing without it.
    assert 'id="m-paste"' in markup


def test_every_button_the_script_wires_by_data_attribute_is_there():
    markup, _ = _markup_and_script(theme_server.page())
    for attr, values in (("v", ("gauges", "focus")), ("m", ("level", "gauge")),
                         ("w", ("5", "12", "22")), ("e", ("round", "flat")),
                         ("d", ("on", "off"))):
        for v in values:
            assert f'data-{attr}="{v}"' in markup, (attr, v)


def test_the_page_follows_the_computer_and_is_usable_without_a_mouse():
    # Dark first, as a new board is; light when the computer asks for it;
    # a ring on whatever the keyboard is on; and no motion for someone who
    # turned it off.
    markup, _ = _markup_and_script(theme_server.page())
    assert "@media (prefers-color-scheme: light)" in markup
    assert "@media (prefers-reduced-motion: reduce)" in markup
    assert ":focus-visible" in markup
    assert "min-height: 44px" in markup


def test_the_page_has_no_outside_resources():
    page = theme_server.page()
    for needle in ("http://", "https://", "//fonts.", "cdn", "@import"):
        if needle == "http://":
            # Only the SVG namespace may appear.
            rest = page.replace("http://www.w3.org/2000/svg", "")
            assert "http://" not in rest
        else:
            assert needle not in page.lower()


def _page_builtins(page):
    line = next(l for l in page.splitlines() if "var BUILTINS = " in l)
    return json.loads(line.split("var BUILTINS = ", 1)[1].rstrip().rstrip(";"))


def test_the_page_names_every_built_in_theme_in_id_order():
    # As the board's tiles name them: the page said "Colour-blind safe"
    # beside a board that says "Colour-blind" (audit D3, 2026-10-28).
    rows = _page_builtins(theme_server.page())
    assert [r["id"] for r in rows] == list(range(12))
    assert [r["name"] for r in rows] == [
        "Dark", "Neon", "Bold", "Mono", "Ocean", "Synthwave", "Forest",
        "Retro LCD", "Colour-blind", "Light", "Light soft", "Light ink"]
    assert [r["cli"] for r in rows] == [theme.display_name(i)
                                        for i in range(12)]


def test_no_name_can_reach_the_script_element_as_markup(monkeypatch):
    # Every "<" leaves as <, not only "</" (review NICE 3): "<!--"
    # would otherwise put the HTML parser into its escaped script state.
    odd = "</script><!--<script>"
    rows = [dict(b) for b in theme.theme_builtins.BUILTINS]
    rows[0]["name"] = odd
    monkeypatch.setattr(theme.theme_builtins, "BUILTINS", tuple(rows))
    text = theme_server.builtins_json()
    assert "<" not in text
    assert json.loads(text)[0]["name"] == odd
    assert "<" not in _page_builtins_line(theme_server.page())


def _page_builtins_line(page):
    return next(l for l in page.splitlines() if "var BUILTINS = " in l
                ).split("var BUILTINS = ", 1)[1]


def test_the_served_page_has_no_placeholder_left():
    page = theme_server.page()
    assert theme_server.BUILTINS_MARK not in page
    assert "@BUILTINS@" not in page


def test_a_page_with_nowhere_for_the_themes_is_a_broken_install(
        tmp_path, monkeypatch):
    # The page is read from beside the module; one that cannot hold the
    # presets must fail like a missing one, which the CLI reports, rather
    # than serve a builder whose script stops on its first line.
    (tmp_path / theme_server.PAGE_FILE).write_text("<html></html>",
                                                   encoding="utf-8")
    monkeypatch.setattr(theme_server, "__file__",
                        str(tmp_path / "theme_server.py"))
    with pytest.raises(FileNotFoundError):
        theme_server.page()


def test_the_page_ships_in_the_binary():
    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    with open(os.path.join(root, "tools", "build_binary.sh"),
              encoding="utf-8") as f:
        assert "pc/theme_builder.html" in f.read()


# ------------------------------------------------------------- opening

class Recorder:
    def __init__(self, result=True):
        self.calls = []
        self.result = result

    def __call__(self, *a, **k):
        self.calls.append((a, k))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_mac_and_windows_open_through_webbrowser():
    for plat in ("darwin", "win32"):
        browser = Recorder(True)
        assert theme_server.open_url("http://127.0.0.1:1/open?k=x",
                                     platform=plat, environ={},
                                     browser=browser)
        assert browser.calls[0][0] == ("http://127.0.0.1:1/open?k=x",)


def test_a_browser_that_will_not_start_is_reported():
    assert not theme_server.open_url("u", platform="darwin", environ={},
                                     browser=Recorder(False))
    assert not theme_server.open_url("u", platform="win32", environ={},
                                     browser=Recorder(OSError("no")))


def test_linux_with_a_display_uses_xdg_open_detached():
    popen = Recorder(object())
    ok = theme_server.open_url(
        "u", platform="linux", environ={"DISPLAY": ":0", "PATH": "/usr/bin"},
        which=lambda n: "/usr/bin/" + n, popen=popen,
        runner=Recorder(""), browser=Recorder(True))
    assert ok
    (argv,), kw = popen.calls[0]
    assert argv == ["/usr/bin/xdg-open", "u"]
    assert kw["env"]["DISPLAY"] == ":0"
    assert kw["start_new_session"] is True


def test_a_systemd_service_borrows_the_desktops_display():
    # A systemd --user unit starts with the manager's environment, which has
    # no DISPLAY until the desktop imports it; show-environment is where it
    # then lives.
    env_out = ("HOME=/home/a\nWAYLAND_DISPLAY=wayland-0\n"
               "XDG_RUNTIME_DIR=/run/user/1000\n"
               "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus\n")
    runner = Recorder(env_out)
    popen = Recorder(object())
    ok = theme_server.open_url("u", platform="linux",
                               environ={"PATH": "/usr/bin"},
                               which=lambda n: "/usr/bin/" + n,
                               popen=popen, runner=runner,
                               browser=Recorder(True))
    assert ok
    assert runner.calls[0][0][0] == ["systemctl", "--user",
                                     "show-environment"]
    env = popen.calls[0][1]["env"]
    assert env["WAYLAND_DISPLAY"] == "wayland-0"
    assert env["DBUS_SESSION_BUS_ADDRESS"].startswith("unix:")


def test_headless_linux_opens_nothing():
    popen = Recorder(object())
    browser = Recorder(True)
    ok = theme_server.open_url("u", platform="linux",
                               environ={"PATH": "/usr/bin", "TERM": "xterm"},
                               which=lambda n: "/usr/bin/" + n,
                               popen=popen, runner=Recorder(None),
                               browser=browser)
    assert not ok
    # Not even a text browser in the service's own terminal-less session.
    assert popen.calls == [] and browser.calls == []


def test_linux_without_xdg_open_falls_back_to_webbrowser():
    browser = Recorder(True)
    assert theme_server.open_url("u", platform="linux",
                                 environ={"DISPLAY": ":1"},
                                 which=lambda n: None, popen=Recorder(),
                                 runner=Recorder(""), browser=browser)
    assert browser.calls


# ------------------------------------------------------------- launcher

class FakeServer:
    made = []

    def __init__(self, home):
        self.home = home
        self.done = False
        self.links = 0
        self.finished = None
        self.alive = False
        self.touches = 0
        FakeServer.made.append(self)

    def start(self):
        pass

    def new_link(self):
        self.links += 1
        return f"http://127.0.0.1:1/open?k={self.links}"

    def page_alive(self):
        return self.alive

    def touch(self):
        self.touches += 1

    def finish(self, outcome, cancel=True):
        self.done = True
        self.finished = (outcome, cancel)


def launcher_with(home, opener, clock=None):
    FakeServer.made = []
    clock = clock or Clock()
    lau = theme_server.Launcher(home, opener=opener, make_server=FakeServer,
                                now=clock)
    return lau, clock


def test_the_launcher_starts_one_server_and_reopens_it(home):
    opened = []
    lau, clock = launcher_with(home, lambda u: opened.append(u) or True)
    assert lau.launch()
    clock.t += theme_server.OPEN_EVERY_S
    assert lau.launch()
    assert len(FakeServer.made) == 1
    assert opened == ["http://127.0.0.1:1/open?k=1",
                      "http://127.0.0.1:1/open?k=2"]


def test_an_ask_repeated_within_two_seconds_is_the_same_ask(home):
    opened = []
    lau, clock = launcher_with(home, lambda u: opened.append(u) or True)
    assert lau.launch()
    clock.t += theme_server.OPEN_EVERY_S - 1
    # Answered with what the open already under way achieved.
    assert lau.launch()
    assert len(opened) == 1
    # Under the 3 s the board shows "Opened on your computer" for, so a
    # press after the button comes back always opens again (owner,
    # 2026-10-29); it was 10 s, which answered that press with no window.
    assert theme_server.OPEN_EVERY_S == 2
    clock.t += 2
    assert lau.launch()
    assert len(opened) == 2


def test_an_open_page_is_shown_again_on_a_fresh_link(home):
    # It used to be answered ok with no window at all (contract Addendum
    # 6 forbids that): a tab behind other windows is one the owner cannot
    # find. Same builder, a new one-time link, a browser told to show it.
    opened = []
    lau, clock = launcher_with(home, lambda u: opened.append(u) or True)
    lau.launch()
    FakeServer.made[0].alive = True
    clock.t += 300
    assert lau.launch()
    assert len(FakeServer.made) == 1
    assert opened == ["http://127.0.0.1:1/open?k=1",
                      "http://127.0.0.1:1/open?k=2"]


def test_the_launcher_gives_up_quietly_with_no_browser(home):
    lau, _ = launcher_with(home, lambda u: False)
    assert not lau.launch()
    # Shut without a cancel: nothing was previewed, the board has its own
    # fallback on screen, and a stray cancel would be the only line sent.
    assert FakeServer.made[0].finished == ("no browser", False)


def test_the_launcher_starts_afresh_after_a_finished_builder(home):
    lau, clock = launcher_with(home, lambda u: True)
    lau.launch()
    FakeServer.made[0].done = True
    clock.t += theme_server.OPEN_EVERY_S
    lau.launch()
    assert len(FakeServer.made) == 2


def test_closing_the_launcher_cancels_a_live_builder(home):
    lau, _ = launcher_with(home, lambda u: True)
    lau.launch()
    lau.close()
    assert FakeServer.made[0].finished == ("daemon stopping", True)


def test_a_missing_page_is_a_no_not_a_crash(home, monkeypatch):
    monkeypatch.setattr(theme_server, "PAGE_FILE", "not-there.html")
    opened = []
    lau = theme_server.Launcher(home, opener=lambda u: opened.append(u)
                                or True)
    assert not lau.launch()
    assert opened == []


def test_the_real_launcher_serves_a_real_page(home):
    seen = []
    launcher = theme_server.Launcher(home, opener=lambda u: seen.append(u)
                                     or True)
    try:
        assert launcher.launch()
        link = urllib.parse.urlsplit(seen[0])
        assert link.hostname == "127.0.0.1"
        c = http.client.HTTPConnection("127.0.0.1", link.port, timeout=5)
        c.request("GET", link.path + "?" + link.query)
        assert c.getresponse().status == 303
        c.close()
    finally:
        launcher.close()


# ------------------------------------------------------------- hardening

def raw_request(server, data):
    s = socket.create_connection(("127.0.0.1", server.port), timeout=5)
    s.sendall(data)
    out = b""
    try:
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            out += chunk
    except socket.timeout:
        pass
    s.close()
    return out


def test_a_non_ascii_token_is_refused_quietly(server, capsys):
    link = urllib.parse.urlsplit(server.new_link())
    host = f"127.0.0.1:{server.port}".encode()
    out = raw_request(server, b"GET /open?k=%C3%A9" + b" HTTP/1.1\r\nHost: "
                      + host + b"\r\nConnection: close\r\n\r\n")
    assert out.startswith(b"HTTP/1.0 403") or out.startswith(b"HTTP/1.1 403")
    out = raw_request(server, b"GET / HTTP/1.1\r\nHost: " + host
                      + b"\r\nCookie: " + theme_server.cookie_name(
                          server.port).encode() + b"=\xc3\xa9\xff"
                      + b"\r\nConnection: close\r\n\r\n")
    assert b" 403" in out.split(b"\r\n", 1)[0]
    assert "Traceback" not in capsys.readouterr().err
    # The real link still works afterwards.
    assert request(server, "GET", link.path + "?" + link.query)[0] == 303


def test_a_handler_error_never_reaches_stderr(server, capsys, monkeypatch):
    def boom(self):
        raise RuntimeError("inside a handler")
    monkeypatch.setattr(theme_server._Handler, "do_GET", boom)
    raw_request(server, b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                b"Connection: close\r\n\r\n")
    err = capsys.readouterr().err
    assert "Traceback" not in err and "inside a handler" not in err


def test_a_stalled_client_is_dropped():
    assert theme_server._Handler.timeout == 10


def test_the_cookie_is_named_for_its_port(server):
    link = urllib.parse.urlsplit(server.new_link())
    _, headers, _ = request(server, "GET", link.path + "?" + link.query)
    assert headers["Set-Cookie"].startswith(f"blink_theme_{server.port}=")


def test_a_failed_save_is_reported_to_the_page(server, home, monkeypatch):
    cookie = login(server)

    def refuse(*a, **k):
        raise PermissionError(13, "denied")
    monkeypatch.setattr(theme, "save", refuse)
    status, _, body = request(server, "POST", "/api/save", {"id": 3},
                              cookie=cookie)
    assert status == 500
    assert "not saved" in json.loads(body)["error"].lower()
    assert not server.done


def test_save_leaves_an_unreadable_theme_json_alone(server, home):
    # Round-2 review, R2: the builder's own Unreadable branch, with a real
    # theme.json that does not parse rather than a patched save().
    from tests.support.home import inside
    os.makedirs(home, exist_ok=True)
    path = inside(home, theme.theme_path(home))
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"id": 4, "owner": "text"')
    cookie = login(server)
    status, _, body = request(server, "POST", "/api/save", {"id": 3},
                              cookie=cookie)
    assert status == 500
    err = json.loads(body)["error"]
    assert err.startswith("Not saved: ")
    assert "left as it is" in err
    assert "owner" not in err
    assert not server.done                 # Save can be tried again
    with open(path, encoding="utf-8") as f:
        assert f.read() == '{"id": 4, "owner": "text"'
    assert theme.take_preview(home) is None   # and no cancel was written


def test_the_page_heartbeat_keeps_it_alive_without_counting_as_an_edit(
        server):
    cookie = login(server)
    assert server.page_alive()
    server.clock.t += theme_server.PAGE_ALIVE_S + 1
    assert not server.page_alive()
    status, _, _ = request(server, "POST", "/api/ping", b"", cookie=cookie)
    assert status == 200
    assert server.page_alive()
    server.clock.t += theme_server.IDLE_S - theme_server.PAGE_ALIVE_S
    request(server, "POST", "/api/ping", b"", cookie=cookie)
    server.tick()
    assert server.wait(5) == "idle"


def test_close_does_not_wait_on_a_browser_that_is_still_opening(home):
    # launchd allows 20 s between SIGTERM and SIGKILL, and burn.sh waits 5.
    # A close() that queued behind an opener stuck starting a browser took
    # 28.7 s (review, 2026-10-27).
    import threading as _t
    import time as _time
    release = _t.Event()
    entered = _t.Event()

    def stuck(url):
        entered.set()
        release.wait(10)
        return True
    lau, _ = launcher_with(home, stuck)
    worker = _t.Thread(target=lau.launch, daemon=True)
    worker.start()
    assert entered.wait(5)
    t0 = _time.monotonic()
    lau.close()
    assert _time.monotonic() - t0 < 2
    assert FakeServer.made[0].finished == ("daemon stopping", True)
    release.set()
    worker.join(5)
