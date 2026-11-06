"""The settings page's endpoints on the local server (spec 8.3, D35).

Real loopback server, a fake BoardOps. No browser, no board.
"""
import json
import os

import pytest

from pc import settings_ops, theme_server, user_config
from tests.pc.test_theme_server import Clock, login, request

PW = "hunter2-hunter2"
SSID = b"home net".hex()


class FakeOps:
    def __init__(self):
        self.joins = []
        self.fail = None

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

    assert get() == {"status_page": False, "auto_update": True}
    assert post(server, "/api/computer", {"status_page": True}, c)[0] == 200
    assert user_config.load(home)["status_page"] is True
    s, _, body = post(server, "/api/computer", {"auto_update": False}, c)
    assert s == 200 and os.path.exists(os.path.join(home, "no-auto-update"))
    assert get() == {"status_page": True, "auto_update": False}
    post(server, "/api/computer", {"auto_update": True, "status_page": False},
         c)
    assert get() == {"status_page": False, "auto_update": True}
    assert not os.path.exists(os.path.join(home, "no-auto-update"))
    assert post(server, "/api/computer", {"status_page": "yes"}, c)[0] == 400
    assert post(server, "/api/computer", {}, c)[0] == 400
