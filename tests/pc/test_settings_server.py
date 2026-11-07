"""The settings page's endpoints on the local server (spec 8.3, D35).

Real loopback server, a fake BoardOps. No browser, no board.
"""
import http.client
import json
import os
import socket
import struct
import threading
import zlib
import time
import urllib.parse

import pytest

from pc import logo_encode, logo_preview, settings_ops, theme_server, user_config
from tests.pc.test_theme_server import Clock, login, request
from tests.support import gen_gif

PW = "hunter2-hunter2"
SSID = b"home net".hex()


class FakeOps:
    def __init__(self):
        self.joins = []
        self.fail = None
        self.logos = []
        self.removes = 0
        self.prefs = []
        self.logo_state = {"t": "logo_state", "present": 1, "bytes": 1,
                           "frames": 1}

    def state(self):
        return {"st": "idle", "link": "usb", "usb": True}

    def device(self):
        return {"fw": "1.5.0", "link": "usb"}

    def scan(self):
        return [{"s": SSID, "r": -40, "sec": 3}]

    def join(self, ssid, password):
        self.joins.append((ssid, password))
        if self.fail:
            raise settings_ops.BoardOpError(self.fail)
        return {"ok": True}

    def forget(self):
        return {"ok": True}

    def logo(self, blob):
        self.logos.append(blob)
        if self.fail:
            raise settings_ops.BoardOpError(self.fail)
        return self.logo_state

    def logo_remove(self):
        self.removes += 1
        return {"t": "logo_state", "present": 0}

    def pref_set(self, **fields):
        self.prefs.append(fields)
        if self.fail:
            raise settings_ops.BoardOpError(self.fail)
        return dict({"provider": "claude", "focus": 0, "bright": 100,
                     "view": 0}, **{k: v for k, v in fields.items()
                                    if k != "main_src"})


@pytest.fixture
def home(tmp_path):
    return str(tmp_path / ".blink")


@pytest.fixture
def ops():
    return FakeOps()


def make(home, **kw):
    s = theme_server.BuilderServer(home, now=Clock(), tick_s=3600, **kw)
    s.start()
    return s


@pytest.fixture
def server(home, ops):
    s = make(home, board_ops=ops)
    yield s
    s.finish("test over")
    s.wait(5)


def own(server):
    return f"http://127.0.0.1:{server.port}"


def post(server, path, body, cookie, **kw):
    return request(server, "POST", path, body, cookie=cookie, **kw)


def test_wifi_routes_need_the_session_cookie(server):
    for m, p in (("GET", "/api/wifi"), ("GET", "/api/wifi/nets"),
                 ("GET", "/api/device"), ("GET", "/api/computer"),
                 ("POST", "/api/wifi/scan"), ("POST", "/api/wifi/join"),
                 ("POST", "/api/wifi/forget"), ("POST", "/api/computer")):
        assert request(server, m, p, {} if m == "POST" else None,
                       origin=own(server))[0] == 403


@pytest.mark.parametrize("origin,want", [("absent", 403), ("null", 403),
                                         ("http://evil.test", 403),
                                         (None, 200)])
def test_posts_need_an_origin(server, origin, want):
    c = login(server)
    assert post(server, "/api/wifi/forget", {}, c, origin=origin)[0] == want


def test_gets_work_without_an_origin(server):
    c = login(server)
    assert request(server, "GET", "/api/wifi", cookie=c)[0] == 200
    assert request(server, "GET", "/api/device", cookie=c)[0] == 200
    assert request(server, "GET", "/api/wifi/nets", cookie=c)[0] == 200


def test_json_posts_need_json(server):
    c = login(server)
    s, h, _ = post(server, "/api/wifi/forget", b"a=b", c,
                   ctype="application/x-www-form-urlencoded")
    assert s == 415
    s, h, _ = request(server, "OPTIONS", "/api/wifi/join", cookie=c,
                      origin=own(server))
    assert not any(k.lower().startswith("access-control") for k in h)


def test_wrong_host_refused(server):
    c = login(server)
    bad = f"evil.test:{server.port}"
    assert request(server, "GET", "/api/wifi", cookie=c, host=bad)[0] in (
        400, 403)
    assert post(server, "/api/wifi/forget", {}, c, host=bad)[0] in (400, 403)


@pytest.mark.parametrize("pw", ["short", "x" * 65, "pässword12",
                                "z" * 64])
def test_join_validates_before_touching_the_board(server, ops, pw):
    c = login(server)
    s, _, body = post(server, "/api/wifi/join", {"s": SSID, "p": pw}, c)
    assert s == 400 and json.loads(body)["why"]
    assert ops.joins == []
    assert post(server, "/api/wifi/join", {"s": "zz", "p": PW}, c)[0] == 400
    assert post(server, "/api/wifi/join", {"s": "", "p": PW}, c)[0] == 400
    assert ops.joins == []


def test_join_and_scan_reach_the_board(server, ops):
    c = login(server)
    s, _, body = post(server, "/api/wifi/join", {"s": SSID, "p": PW}, c)
    assert s == 200 and ops.joins == [(b"home net", PW)]
    post(server, "/api/wifi/scan", {}, c)
    nets = json.loads(request(server, "GET", "/api/wifi/nets", cookie=c)[2])
    assert nets["nets"][0]["s"] == SSID
    ops.fail = "The board did not answer."
    s, _, body = post(server, "/api/wifi/join", {"s": SSID, "p": PW}, c)
    assert s == 409 and json.loads(body)["why"] == ops.fail


def test_password_not_in_any_response_or_state(server, ops, capsys):
    c = login(server)
    seen = [post(server, "/api/wifi/join", {"s": SSID, "p": PW}, c)[2]]
    ops.fail = "nope"
    seen.append(post(server, "/api/wifi/join", {"s": SSID, "p": PW}, c)[2])
    seen.append(post(server, "/api/wifi/join", {"s": SSID, "p": "bad"}, c)[2])
    for p in ("/api/wifi", "/api/wifi/nets", "/api/device", "/api/state"):
        seen.append(request(server, "GET", p, cookie=c)[2])
    assert all(PW.encode() not in b for b in seen)
    assert PW not in repr(vars(server))


def test_nothing_logged(server, capsys):
    c = login(server)
    post(server, "/api/wifi/join", {"s": SSID, "p": PW}, c)
    post(server, "/api/wifi/join", b"{not json", c)
    out = capsys.readouterr()
    assert PW not in out.out + out.err and SSID not in out.out + out.err
    assert out.out == "" and out.err == ""


def test_theme_only_server_says_use_blink_settings(home):
    s = make(home)
    try:
        c = login(s)
        for m, p in (("GET", "/api/wifi"), ("GET", "/api/device"),
                     ("GET", "/api/logo"), ("GET", "/api/pref"),
                     ("POST", "/api/wifi/join"), ("POST", "/api/logo")):
            st, _, body = request(s, m, p, {} if m == "POST" else None,
                                  cookie=c)
            assert st == 409 and "blink settings" in json.loads(body)["why"]
    finally:
        s.finish("test over")
        s.wait(5)


TOK = "t" * 40
LATE = "l" * 40


def test_preissued_token_is_one_time(home, ops):
    s = make(home, board_ops=ops, token=TOK)
    try:
        assert request(s, "GET", "/open?k=" + TOK + "")[0] == 303
        assert request(s, "GET", "/open?k=" + TOK + "")[0] == 403
    finally:
        s.finish("test over")
        s.wait(5)


def test_add_token_on_a_running_server(server):
    assert request(server, "GET", f"/open?k={LATE}")[0] == 403
    server.add_token(LATE)
    assert request(server, "GET", f"/open?k={LATE}")[0] == 303
    assert request(server, "GET", f"/open?k={LATE}")[0] == 403


@pytest.mark.parametrize("bad", ["", "short", "x" * 31, None, b"x" * 40, 7])
def test_a_short_or_non_str_token_is_refused(server, home, ops, bad):
    with pytest.raises(ValueError):
        server.add_token(bad)
    if bad is not None:        # None is "no pre-issued token"
        with pytest.raises(ValueError):
            make(home, board_ops=ops, token=bad)


def test_computer_settings_round_trip(server, home):
    c = login(server)

    def get():
        return json.loads(request(server, "GET", "/api/computer", cookie=c)[2])

    assert get() == {"status_page": False, "auto_update": True,
                     "usage_refresh": False}
    assert post(server, "/api/computer", {"status_page": True}, c)[0] == 200
    assert user_config.load(home)["status_page"] is True
    s, _, body = post(server, "/api/computer", {"auto_update": False}, c)
    assert s == 200 and os.path.exists(os.path.join(home, "no-auto-update"))
    assert get() == {"status_page": True, "auto_update": False,
                     "usage_refresh": False}
    post(server, "/api/computer", {"auto_update": True, "status_page": False},
         c)
    assert get() == {"status_page": False, "auto_update": True,
                     "usage_refresh": False}
    assert not os.path.exists(os.path.join(home, "no-auto-update"))
    assert post(server, "/api/computer", {"status_page": "yes"}, c)[0] == 400
    assert post(server, "/api/computer", {}, c)[0] == 400



def test_keep_figures_fresh_is_the_usage_refresh_setting(server, home,
                                                         monkeypatch):
    """The page's switch is `blink config usage-refresh`: the same key, and
    on is the same fresh start (usage_refresh.switch_on), so a refresh that
    turned itself off for a reason does not stay off for it."""
    from pc import usage_refresh
    calls = []
    monkeypatch.setattr(usage_refresh, "switch_on",
                        lambda bh, h: calls.append(bh))
    c = login(server)
    s, _, body = post(server, "/api/computer", {"usage_refresh": True}, c)
    assert s == 200 and json.loads(body)["usage_refresh"] is True
    assert user_config.load(home)[usage_refresh.SETTING] is True
    assert calls == [home]
    s, _, body = post(server, "/api/computer", {"usage_refresh": False}, c)
    assert s == 200 and json.loads(body)["usage_refresh"] is False
    assert user_config.load(home)[usage_refresh.SETTING] is False
    assert calls == [home]          # off is only the setting
    assert post(server, "/api/computer", {"usage_refresh": 1}, c)[0] == 400


# ------------------------------------------------------------ the logo route

RAW = {"Content-Type": "application/octet-stream"}


def _logo_headers(kind="rgba", hold="3000", fit="fill", bg="102030"):
    h = {"X-Blink-Logo": kind, "X-Blink-Hold-Ms": hold, "X-Blink-Bg": bg}
    if fit:
        h["X-Blink-Fit"] = fit
    return h


def _logo(server, c, body, **kw):
    return request(server, "POST", "/api/logo", body, cookie=c,
                   ctype="application/octet-stream",
                   extra=_logo_headers(**kw))


def _gif(frames=2, comment=0, size=(320, 240), colour=(255, 255, 255)):
    pal = [(0, 0, 0), colour]
    fs = [gen_gif.frame(size[0], size[1], bytes([1]) * (size[0] * size[1]),
                        delay_cs=10) for _ in range(frames)]
    img = gen_gif.build(size[0], size[1], pal, fs)
    if comment:
        img = img[:-1] + b"\x21\xfe" + gen_gif._blocks(b"x" * comment) + b";"
    return img


def test_logo_rgba_full_size_succeeds_through_the_real_handler(server, ops):
    c = login(server)
    raw = bytes([9, 8, 7, 255]) * (320 * 240)
    s, _, body = _logo(server, c, raw)
    assert s == 200 and json.loads(body)["logo"]["present"] == 1
    assert len(ops.logos) == 1
    assert ops.logos[0] == logo_encode.from_rgba(raw, (16, 32, 48), 3000)


def test_logo_gif_1_5_mib_succeeds(server, ops):
    c = login(server)
    g = _gif(comment=1_500_000)
    assert len(g) > 1_500_000
    s, _, body = _logo(server, c, g, kind="gif")
    assert s == 200 and len(ops.logos) == 1


def test_logo_over_2_mib_is_413_before_reading(server, ops):
    c = login(server)
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    conn.putrequest("POST", "/api/logo")
    h = dict(RAW, Host=f"127.0.0.1:{server.port}", Cookie=c,
             Origin=f"http://127.0.0.1:{server.port}",
             **_logo_headers())
    h["Content-Length"] = str(2 * 1024 * 1024 + 1)
    for k, v in h.items():
        conn.putheader(k, v)
    conn.endheaders()                       # no body is ever sent
    assert conn.getresponse().status == 413
    conn.close()
    assert ops.logos == []


def test_other_routes_keep_4096(server):
    c = login(server)
    big = {"s": SSID, "p": "x" * 5000}
    assert post(server, "/api/wifi/join", big, c)[0] == 413
    assert post(server, "/api/pref", {"bright": 40, "pad": "x" * 5000},
                c)[0] == 413
    assert post(server, "/api/preview", {"id": 0, "pad": "x" * 5000},
                c)[0] == 413


def test_logo_post_needs_exactly_320x240x4_for_rgba(server, ops):
    c = login(server)
    for n in (0, 307199, 307201, 4):
        s, _, body = _logo(server, c, bytes(n))
        assert s == 400 and json.loads(body)["why"], n
    assert ops.logos == []


def test_logo_post_needs_origin(server, ops):
    c = login(server)
    for origin in ("absent", "null", "http://evil.test"):
        assert request(server, "POST", "/api/logo", bytes(307200), cookie=c,
                       origin=origin, ctype="application/octet-stream",
                       extra=_logo_headers())[0] == 403
    assert request(server, "POST", "/api/logo", bytes(307200),
                   ctype="application/octet-stream",
                   origin=own(server), extra=_logo_headers())[0] == 403
    assert ops.logos == []


def _foreign_logo_post(server, c, length):
    """A raw socket that has sent the head of a logo POST from another site:
    do_POST refuses it with 403 before a byte of the body is read."""
    sock = socket.create_connection(("127.0.0.1", server.port), timeout=10)
    head = (f"POST /api/logo HTTP/1.1\r\nHost: 127.0.0.1:{server.port}\r\n"
            f"Cookie: {c}\r\nOrigin: http://evil.test\r\n"
            f"Content-Type: application/octet-stream\r\n"
            f"Content-Length: {length}\r\n\r\n")
    sock.sendall(head.encode())
    return sock


def _record_linger(monkeypatch):
    """What each closing connection's drain read, and how long it took, by
    the client's port: login()'s own connection closes through it too."""
    seen, real = {}, theme_server._linger

    def linger(request):
        peer = request.getpeername()[1]
        t = time.monotonic()
        n = real(request)
        seen[peer] = (n, time.monotonic() - t)
        return n
    monkeypatch.setattr(theme_server, "_linger", linger)
    return seen


def _keep_sending(sock, chunk, pause, secs=10.0):
    """Send `chunk` every `pause` s until the server ends the connection
    (or `secs`, the test's own guard), on a thread."""
    def run():
        end = time.monotonic() + secs
        try:
            while time.monotonic() < end:
                sock.sendall(chunk)
                if pause:
                    time.sleep(pause)
        except OSError:
            pass
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def _wait_for(seen, sock, secs):
    """(bytes drained, seconds) for this client's connection, or None."""
    port = sock.getsockname()[1]
    end = time.monotonic() + secs
    while port not in seen and time.monotonic() < end:
        time.sleep(0.02)
    return seen.get(port)


def test_a_refused_flood_is_drained_no_further_than_one_logo(
        server, monkeypatch):
    """A refused client that never stops sending: the drain ends at one logo
    body plus 64 KiB, well inside its time bound, and the connection ends."""
    seen = _record_linger(monkeypatch)
    sock = _foreign_logo_post(server, login(server), 64 * 1024 * 1024)
    t = _keep_sending(sock, bytes(65536), 0)
    try:
        got = _wait_for(seen, sock, 5)
        assert got, "the connection never closed"
        n, secs = got
        # It was the byte bound that ended it, and it held.
        assert theme_server.MAX_LOGO_BODY < n <= theme_server.MAX_LOGO_BODY + 65536
        assert secs < 3.0
        t.join(5)
        assert not t.is_alive()      # the server's close ended the sender
    finally:
        sock.close()


def test_a_refused_trickle_is_drained_for_two_seconds_at_most(
        server, monkeypatch):
    """A refused client that sends slowly, never reaching the byte bound:
    the time bound alone ends it."""
    seen = _record_linger(monkeypatch)
    sock = _foreign_logo_post(server, login(server), 64 * 1024 * 1024)
    t = _keep_sending(sock, bytes(1024), 0.05)
    try:
        got = _wait_for(seen, sock, 8)
        assert got, "the connection never closed"
        n, secs = got
        assert 0 < n < theme_server.MAX_LOGO_BODY and 1.5 < secs < 3.0
    finally:
        sock.close()
        t.join(11)


def test_a_client_that_stops_sending_reads_the_refusal(server, monkeypatch):
    """The point of the drain: a whole 300 KB body refused unread, and the
    client that sent it still reads the 403 rather than a reset (WinError
    10053 on Windows before the drain)."""
    seen = _record_linger(monkeypatch)
    body = bytes(307200)
    sock = _foreign_logo_post(server, login(server), len(body))
    try:
        sock.sendall(body)
        status = sock.makefile("rb").readline()
        assert status.split()[1:2] == [b"403"], status
        got = _wait_for(seen, sock, 5)
        assert got and got[1] < 3.0
    finally:
        sock.close()


def test_logo_post_needs_octet_stream(server, ops):
    c = login(server)
    s, _, _ = request(server, "POST", "/api/logo", bytes(307200), cookie=c,
                      ctype="application/json", extra=_logo_headers())
    assert s == 415 and ops.logos == []


def test_logo_limits_reported_as_one_sentence(server, ops):
    c = login(server)
    fs = [gen_gif.frame(1, 1, b"\x01", delay_cs=10) for _ in range(181)]
    g = gen_gif.build(320, 240, [(0, 0, 0), (255, 255, 255)], fs)
    s, _, body = _logo(server, c, g, kind="gif")
    why = json.loads(body)["why"]
    assert s == 400 and why == "Too many frames for the board."
    s, _, body = _logo(server, c, b"GIF89a-not-really", kind="gif")
    assert s == 400 and json.loads(body)["why"].endswith(".")
    assert ops.logos == []


def test_logo_hold_and_fit_headers_reach_the_encoder(server, ops):
    c = login(server)
    s, _, _ = _logo(server, c, _gif(), kind="gif", hold="10000",
                    fit="margin", bg="000000")
    assert s == 200
    info = logo_encode.parse(ops.logos[0], frames=True)
    assert info["hold_ms"] == 10000
    row = info["frames"][0][120 * 320:121 * 320]
    assert set(row[:64]) == {0} and row[70] == 0xFFFF   # the 64 px band
    for bad in ({"hold": "30000"}, {"hold": "abc"}, {"fit": "huge"},
                {"fit": None}, {"bg": "12345"}, {"bg": "zzzzzz"}):
        s, _, body = _logo(server, c, _gif(), kind="gif", **bad)
        assert s == 400 and json.loads(body)["why"], bad
    s, _, _ = request(server, "POST", "/api/logo", _gif(), cookie=c,
                      ctype="application/octet-stream",
                      extra={"X-Blink-Logo": "gif", "X-Blink-Fit": "fill",
                             "X-Blink-Bg": "000000"})
    assert s == 400                                      # no hold header
    s, _, _ = _logo(server, c, bytes(307200), kind="png")
    assert s == 400
    assert len(ops.logos) == 1


def test_logo_fit_is_ignored_for_rgba(server, ops):
    c = login(server)
    assert _logo(server, c, bytes([1, 2, 3, 255]) * 76800, fit=None)[0] == 200
    assert _logo(server, c, bytes([1, 2, 3, 255]) * 76800,
                 fit="huge")[0] == 200


def test_logo_remove_and_state(server, ops):
    c = login(server)
    s, _, body = post(server, "/api/logo/remove", {}, c)
    assert s == 200 and ops.removes == 1
    assert json.loads(body)["logo"]["present"] == 0
    assert json.loads(request(server, "GET", "/api/logo",
                              cookie=c)[2]) == {"logo": None}
    ops.device = lambda: {"logo": {"present": 1}}
    assert json.loads(request(server, "GET", "/api/logo",
                              cookie=c)[2])["logo"]["present"] == 1


def test_logo_board_errors_are_409(server, ops):
    c = login(server)
    ops.fail = "The board refused the logo."
    s, _, body = _logo(server, c, bytes(307200))
    assert s == 409 and json.loads(body)["why"] == ops.fail


def test_pref_post_validates_enums(server, ops):
    c = login(server)
    for good in ({"bright": 20}, {"bright": 100}, {"focus": 1},
                 {"main_src": "codex"}, {"view": 0},
                 {"bright": 40, "focus": 0, "main_src": "claude"}):
        s, _, body = post(server, "/api/pref", good, c)
        assert s == 200 and "pref" in json.loads(body), good
    assert [p for p in ops.prefs][:2] == [{"bright": 20}, {"bright": 100}]
    n = len(ops.prefs)
    for bad in ({}, [], "x", {"bright": 30}, {"bright": "40"},
                {"bright": True}, {"focus": 2}, {"focus": True},
                {"main_src": "gemini"}, {"view": -1}, {"view": 256},
                {"view": 1.5}, {"weekly": 1}, {"bright": 40, "x": 1}):
        s, _, body = post(server, "/api/pref", bad, c)
        assert s == 400, bad
    assert len(ops.prefs) == n
    ops.fail = "The board did not accept that setting."
    s, _, body = post(server, "/api/pref", {"bright": 40}, c)
    assert s == 409 and json.loads(body)["why"] == ops.fail


def test_logo_bytes_never_logged(server, ops, capsys):
    c = login(server)
    _logo(server, c, bytes([77, 78, 79, 255]) * 76800)
    _logo(server, c, b"junk")
    post(server, "/api/pref", {"bright": 40}, c)
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_one_session_saves_previews_and_sets_a_board_setting(home, ops):
    s = make(home, board_ops=ops, persistent=True)
    try:
        c = login(s)
        dark = {"id": 0}
        assert post(s, "/api/save", dark, c)[0] == 200
        assert post(s, "/api/preview", {"id": 4}, c)[0] == 200
        st, _, body = post(s, "/api/pref", {"bright": 40}, c)
        assert st == 200 and json.loads(body)["pref"]["bright"] == 40
        assert request(s, "GET", "/api/state", cookie=c)[0] == 200
        assert ops.prefs == [{"bright": 40}]
    finally:
        s.finish("test over")
        s.wait(5)


def test_logo_check_gives_the_encoders_size_and_sends_nothing(server, ops):
    c = login(server)
    raw = bytes([9, 8, 7, 255]) * 76800
    s, _, body = request(server, "POST", "/api/logo?check=1", raw, cookie=c,
                         ctype="application/octet-stream",
                         extra=_logo_headers())
    want = len(logo_encode.from_rgba(raw, (16, 32, 48), 3000))
    assert s == 200 and json.loads(body) == {"bytes": want}
    assert ops.logos == []
    s, _, body = request(server, "POST", "/api/logo?check=1", b"x", cookie=c,
                         ctype="application/octet-stream",
                         extra=_logo_headers())
    assert s == 400


def test_a_non_ascii_digit_hold_is_a_400_not_a_crash(server, ops, capsys):
    c = login(server)
    for hold in ("\u00b2", "\u00b3", "3000.0", "+3000"):
        s, _, body = _logo(server, c, bytes(307200), hold=hold)
        assert s == 400 and json.loads(body)["why"], hold
    assert "Traceback" not in capsys.readouterr().err and ops.logos == []


def test_the_page_the_board_opens_is_the_settings_page(home, ops):
    # Owner, 2026-11-07, board on WiFi: Open on PC on the board opened
    # the page, a brightness click changed nothing and no pref_set was
    # written. The daemon's Launcher was built with board_ops but not
    # persistent, so /api/state said persistent false, the page's prefSet
    # returned without a request, and the first Save ended the page.
    opened = []
    lau = theme_server.Launcher(home, opener=lambda u: opened.append(u)
                                or True, board_ops=ops)
    try:
        assert lau.launch()
        s = lau._server
        link = urllib.parse.urlsplit(opened[0])
        st, headers, _ = request(s, "GET", link.path + "?" + link.query)
        assert st == 303
        c = headers["Set-Cookie"].split(";", 1)[0]
        _, _, body = request(s, "GET", "/api/state", cookie=c)
        assert json.loads(body)["persistent"] is True
        assert post(s, "/api/save", {"id": 0}, c)[0] == 200
        assert not s.done
        st, _, body = post(s, "/api/pref", {"bright": 40}, c)
        assert st == 200 and json.loads(body)["pref"]["bright"] == 40
        assert ops.prefs == [{"bright": 40}]
    finally:
        lau.close()


# ------------------------------------------------ the preview of the board's logo


def _sent(server, ops, c):
    """Send a 320x240 red still through the real handler; the board's answer
    is what a real board would report for it."""
    raw = bytes([255, 0, 0, 255]) * (320 * 240)
    blob = logo_encode.from_rgba(raw, (0, 0, 0), 3000)
    info = logo_encode.parse(blob)
    ops.logo_state = {"t": "logo_state", "present": 1,
                      "bytes": logo_encode.HDR_LEN + info["blob_len"],
                      "frames": info["nframes"]}
    ops.device = lambda: {"fw": "1.5.0", "link": "usb",
                          "logo": dict(ops.logo_state)}
    assert _logo(server, c, raw)[0] == 200


def _preview(server, c):
    return request(server, "GET", "/api/logo/preview", cookie=c)


def test_preview_route_needs_the_session_cookie(server, ops):
    assert request(server, "GET", "/api/logo/preview",
                   origin=own(server))[0] == 403


def test_preview_is_the_png_of_what_was_sent_while_the_board_reports_it(server, ops):
    c = login(server)
    assert _preview(server, c)[0] == 404            # nothing sent yet
    _sent(server, ops, c)
    s, h, body = _preview(server, c)
    assert s == 200 and body[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(body) < logo_preview.MAX_PNG
    w, hh = struct.unpack(">II", body[16:24])
    assert (w, hh) == (320, 240)


def test_preview_is_refused_when_the_board_reports_another_logo(server, ops):
    c = login(server)
    _sent(server, ops, c)
    other = dict(ops.logo_state, bytes=ops.logo_state["bytes"] + 32)
    ops.device = lambda: {"logo": other}
    assert _preview(server, c)[0] == 404
    ops.device = lambda: {"logo": dict(ops.logo_state, present=0)}
    assert _preview(server, c)[0] == 404
    ops.device = lambda: {}                          # an older board
    assert _preview(server, c)[0] == 404


def test_removing_the_logo_deletes_the_copy(server, ops, home):
    c = login(server)
    _sent(server, ops, c)
    assert os.path.exists(os.path.join(home, "private", logo_preview.PNG_NAME))
    s, _, _ = post(server, "/api/logo/remove", {}, c)
    assert s == 200
    for n in (logo_preview.PNG_NAME, logo_preview.META_NAME):
        assert not os.path.exists(os.path.join(home, "private", n))
    assert _preview(server, c)[0] == 404


def test_copy_is_owner_only(server, ops, home):
    _sent(server, ops, login(server))
    mode = os.stat(os.path.join(home, "private", logo_preview.PNG_NAME)).st_mode
    assert mode & 0o077 == 0 or os.name == "nt"


def test_first_frame_of_a_gif_is_what_is_kept():
    blob = logo_encode.from_rgba(bytes([0, 255, 0, 255]) * (320 * 240), (0, 0, 0), 3000)
    png = logo_preview.png_from_565(logo_encode.parse(blob, first_frame=True)["frames"][0])
    assert len(logo_encode.parse(blob, first_frame=True)["frames"]) == 1
    assert zlib.decompress(png[png.index(b"IDAT") + 4:png.index(b"IEND") - 8])[1:4] == bytes([0, 255, 0])
