"""`blink settings` (D37, D50): the daemon registers the token and records
its port with the request id; the CLI opens the browser itself."""
import json
import os
import stat
import threading
import time

import pytest

from pc import cli, private_file, settings_request, theme, theme_server
from tests.support.home import inside

TOK = "k" * 40


def home():
    return cli.blink_home()


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 1.0
        return self.t


def req_path():
    return os.path.join(home(), "private", settings_request.REQUEST_FILE)


def test_request_file_is_owner_only_and_has_no_secret_but_the_token():
    token, rid = settings_request.write_request(home(), "connect")
    p = req_path()
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    obj = json.load(open(p))
    assert set(obj) == {"panel", "k", "id"}
    assert obj == {"panel": "connect", "k": token, "id": rid}
    assert len(token) >= 32


def test_take_request_deletes_it():
    token, rid = settings_request.write_request(home(), "look")
    assert settings_request.take_request(home())["id"] == rid
    assert not os.path.exists(req_path())
    assert settings_request.take_request(home()) is None


def test_take_request_refuses_unknown_panel():
    private_file.write_private(
        home(), settings_request.REQUEST_FILE,
        json.dumps({"panel": "root", "k": TOK, "id": "x"}).encode())
    assert settings_request.take_request(home()) is None
    assert not os.path.exists(req_path())


def test_settings_without_a_daemon_says_so(capsys):
    opened = []
    rc = cli.cmd_settings(type("A", (), {"panel": None})(),
                          opener=opened.append, clock=Clock(),
                          sleep=lambda s: None)
    assert rc == 1 and not opened
    assert "The app is not running. Run blink install, or blink run in" \
           " another terminal." in capsys.readouterr().out
    assert not os.path.exists(req_path())


def daemon(launcher_home, answer):
    """A stand-in daemon: takes the request when the CLI sleeps."""
    def sleep(_):
        req = settings_request.take_request(launcher_home)
        if req:
            answer(req)
    return sleep


def test_settings_opens_the_browser_from_the_cli_and_prints_the_link(capsys):
    opened = []

    def answer(req):
        theme.write_builder(home(), 5555, ack=req["id"])
        seen.append(req)
    seen = []
    rc = cli.cmd_settings(type("A", (), {"panel": None})(),
                          opener=lambda u: opened.append(u) or True,
                          clock=Clock(), sleep=daemon(home(), answer))
    assert rc == 0
    url = f"http://127.0.0.1:5555/open?k={seen[0]['k']}&panel=connect"
    assert opened == [url]
    assert url in capsys.readouterr().out


def test_a_stale_port_record_does_not_open_the_browser(capsys):
    theme.write_builder(home(), 5555)            # a builder the board opened
    opened = []
    rc = cli.cmd_settings(type("A", (), {"panel": "board"})(),
                          opener=opened.append, clock=Clock(),
                          sleep=lambda s: None)
    assert rc == 1 and opened == []


class Fake:
    port = 6000
    done = False

    def __init__(self, home, **kw):
        self.kw = kw
        self.tokens = [kw["token"]] if "token" in kw else []
        self.order = kw.get("order")

    def start(self):
        pass

    def add_token(self, t):
        self.tokens.append(t)

    def touch(self):
        pass


def test_daemon_pickup_never_calls_the_opener():
    calls = []
    lau = theme_server.Launcher(home(), opener=lambda u: calls.append(u),
                                make_server=Fake)
    lau.serve_headless("connect", TOK, "r1")
    assert calls == []
    assert theme.read_builder(home()) == 6000


def test_ack_is_written_after_the_token_is_registered(monkeypatch):
    order = []

    class S(Fake):
        def __init__(self, home, **kw):
            super().__init__(home, **kw)
            order.append("token")

    real = theme.write_builder
    monkeypatch.setattr(theme, "write_builder",
                        lambda *a, **k: (order.append("ack"),
                                         real(*a, **k)))
    lau = theme_server.Launcher(home(), opener=lambda u: True, make_server=S)
    lau.serve_headless("connect", TOK, "r2")
    assert order == ["token", "ack"]
    assert theme.read_builder_ack(home()) == "r2"


def test_pickup_registers_the_token_on_a_running_builder():
    lau = theme_server.Launcher(home(), opener=lambda u: True,
                                make_server=Fake)
    lau.serve_headless("look", TOK, "a")
    first = lau._server
    lau.serve_headless("connect", "z" * 40, "b")
    assert lau._server is first and first.tokens == [TOK, "z" * 40]
    assert theme.read_builder_ack(home()) == "b"


def test_install_opens_nothing_and_says_how(tmp_path, capsys, monkeypatch):
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", lambda *a, **k: pytest.fail("opened"))
    (tmp_path / ".claude").mkdir(exist_ok=True)
    assert cli.main(["install"]) == 0
    out = capsys.readouterr().out.rstrip("\n").splitlines()
    assert out[-1] == ("To set up WiFi and the display, press Open settings"
                       " on the board or run: blink settings")
    assert not os.path.exists(req_path())


def test_theme_edit_opens_look():
    opened = []
    lau = theme_server.Launcher(home(), opener=lambda u: opened.append(u) or True,
                                make_server=Fake)
    Fake.new_link = lambda self, panel=None: (
        "http://127.0.0.1:6000/open?k=1" + (f"&panel={panel}" if panel else ""))
    try:
        assert lau.launch()
    finally:
        del Fake.new_link
    assert opened == ["http://127.0.0.1:6000/open?k=1&panel=look"]


def test_settings_succeeds_while_launch_is_stuck_in_the_opener():
    gate = threading.Event()
    entered = threading.Event()

    def stuck(url):
        entered.set()
        gate.wait(20)
        return True
    lau = theme_server.Launcher(home(), opener=stuck, make_server=Fake)
    Fake.new_link = lambda self, panel=None: (
        "http://127.0.0.1:6000/open?k=1" + (f"&panel={panel}" if panel else ""))
    t = threading.Thread(target=lau.launch, daemon=True)
    try:
        t.start()
        assert entered.wait(5)
        opened = []
        t0 = time.monotonic()

        def daemon_sleep(_):
            req = settings_request.take_request(home())
            if req:
                lau.serve_headless(req["panel"], req["k"], req["id"])
        rc = cli.cmd_settings(type("A", (), {"panel": None})(),
                              opener=lambda u: opened.append(u) or True,
                              clock=Clock(), sleep=daemon_sleep)
        assert rc == 0 and len(opened) == 1
        assert not gate.is_set() and time.monotonic() - t0 < 5
    finally:
        gate.set()
        t.join(5)
        del Fake.new_link


def test_a_consumed_request_with_no_answer_does_not_say_not_running(capsys):
    def eat(_):
        settings_request.take_request(home())
    rc = cli.cmd_settings(type("A", (), {"panel": None})(), opener=lambda u: 1,
                          clock=Clock(), sleep=eat)
    out = capsys.readouterr().out
    assert rc == 1 and "not running" not in out and "did not answer" in out


def test_a_refused_request_file_is_gone_after_one_pass(monkeypatch):
    import claude_usage_bridge as b
    settings_request.write_request(home(), "connect")

    def refuse(*a):
        raise private_file.NotPrivate("x")
    monkeypatch.setattr(private_file, "open_private", refuse)
    import io
    log = io.StringIO()
    b.settings_pickup(home(), None, log)
    b.settings_pickup(home(), None, log)
    assert log.getvalue().count("refused") == 1
    assert not os.path.exists(req_path())


def test_pickup_hands_the_request_to_the_launcher_and_logs_failure():
    import io
    import claude_usage_bridge as b
    got = []

    class L:
        def serve_headless(self, *a):
            got.append(a)
            if len(got) == 2:
                raise ValueError("short")
    log = io.StringIO()
    tok, rid = settings_request.write_request(home(), "board")
    b.settings_pickup(home(), L(), log).join(5)
    settings_request.write_request(home(), "look")
    b.settings_pickup(home(), L(), log).join(5)
    assert got[0] == ("board", tok, rid) and len(got) == 2
    assert "request served" in log.getvalue()
    assert "request failed" in log.getvalue()
    assert tok not in log.getvalue()


def test_a_refused_request_folder_is_said_once_per_process(monkeypatch):
    import claude_usage_bridge as b
    import io

    def refuse(*a):
        raise private_file.NotPrivate("x")
    monkeypatch.setattr(b.settings_request, "take_request", refuse)
    monkeypatch.setattr(b, "_said_request_refused", False)
    log = io.StringIO()
    for _ in range(5):
        b.settings_pickup(home(), None, log)
    assert log.getvalue().count("refused") == 1
