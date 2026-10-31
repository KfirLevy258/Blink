"""Why a builder ended, in the daemon's log.

2026-10-31, the owner's board: a builder opened from the board ended 6 s
later, and the log said only "the builder ended; told the board". A Save, a
closed tab, a Cancel and the idle limit all read the same there, and the
installed page's one heading ("This tab is no longer active.") fit every
one of them, so the report "it ends by itself" could not be told from a
Save nobody remembered. The outcome and a count of each kind of request
tell them apart: numbers and a fixed word, never a request's contents.
"""
import re
import urllib.parse

from pc import theme_server
from tests.support import builder_page

# The fixtures are the server tests' own.
from tests.pc.test_theme_server import (  # noqa: F401
    DARKISH, home, login, request, server)

ENDED = re.compile(r"\[theme\] builder ended: (?P<why>[a-z ]+) after "
                   r"(?P<s>\d+) s \(pings=(?P<pings>\d+) "
                   r"previews=(?P<previews>\d+) saves=(?P<saves>\d+) "
                   r"cancels=(?P<cancels>\d+) closes=(?P<closes>\d+) "
                   r"tabs=(?P<tabs>\d+)\)")


def test_an_end_names_its_outcome_and_what_led_to_it(server):
    lines = []
    server.say = lines.append
    cookie = login(server)
    request(server, "POST", "/api/ping", {"tab": "t1"}, cookie=cookie)
    request(server, "POST", "/api/ping", {"tab": "t1"}, cookie=cookie)
    request(server, "POST", "/api/closed", {"tab": "t1"}, cookie=cookie)
    server.clock.t += 6
    server.finish("closed")
    assert len(lines) == 1
    m = ENDED.fullmatch(lines[0])
    assert m, lines[0]
    assert m["why"] == "closed" and m["s"] == "6"
    assert (m["pings"], m["previews"], m["saves"], m["cancels"],
            m["closes"], m["tabs"]) == ("2", "0", "0", "0", "1", "1")


def test_a_save_is_told_from_a_close_and_from_an_edited_save(server):
    # A builder that ended "saved" with previews=0 was Saved with nothing
    # changed in the page; one with previews > 0 was edited first.
    lines = []
    server.say = lines.append
    cookie = login(server)
    request(server, "POST", "/api/ping", {"tab": "t1"}, cookie=cookie)
    request(server, "POST", "/api/save", {"id": 1}, cookie=cookie,
            extra={theme_server.TAB_HEADER: "t1"})
    server.wait(5)
    m = ENDED.fullmatch(lines[0])
    assert m and m["why"] == "saved"
    assert (m["saves"], m["previews"], m["closes"]) == ("1", "0", "0")


def test_the_end_line_holds_no_link_no_cookie_and_no_theme(server):
    lines = []
    server.say = lines.append
    link = server.new_link()
    cookie = login(server)
    request(server, "POST", "/api/preview", DARKISH, cookie=cookie)
    server.finish("cancelled")
    line = lines[0]
    assert ENDED.fullmatch(line)
    token = urllib.parse.parse_qs(urllib.parse.urlsplit(link).query)["k"][0]
    for secret in (token, cookie.split("=", 1)[1], "0e1116", "ba8107"):
        assert secret not in line


def test_a_refused_request_is_not_counted(server):
    lines = []
    server.say = lines.append
    request(server, "POST", "/api/ping", {"tab": "t1"})       # no cookie
    server.finish("closed")
    m = ENDED.fullmatch(lines[0])
    assert m and m["pings"] == "0" and m["tabs"] == "0"


def test_an_end_is_said_once(server):
    lines = []
    server.say = lines.append
    server.finish("idle")
    server.finish("closed")
    assert len(lines) == 1 and "idle" in lines[0]


def test_a_failing_log_does_not_stop_the_builder(server):
    def broken(_):
        raise OSError("stderr is closed")
    server.say = broken
    server.finish("closed")
    assert server.wait(5) == "closed"


def test_the_daemons_builder_says_how_it_ended(home, capsys):
    """The Launcher is the daemon's; its builders log to the daemon's
    stderr, which is bridge.log."""
    lau = theme_server.Launcher(home, opener=lambda url: True)
    try:
        assert lau.launch()
        capsys.readouterr()
        lau._server.finish("closed")
        err = capsys.readouterr().err
        assert ENDED.search(err), err
    finally:
        lau.close()


def test_a_tab_nobody_touches_asks_for_nothing_but_the_state_and_pings():
    """The report was that a builder ends by itself. Run for ten minutes of
    the page's own clock with no input, the page sends one state read and a
    ping every 20 s -- no preview, no save, no cancel, no close -- so
    nothing the page does alone writes the theme or ends the builder. The
    same was measured in Chromium against a real server for 86 s in the
    foreground and 90 s in a background tab (2026-10-31). Restored form
    values, a theme applied from /api/state and a programmatic .value are
    not input events, and none of them reaches Save."""
    r = builder_page.run("""
    await advance(600000);
    return requests.map((q) => q.method + ' ' + q.path);
    """, {"/api/state": {"status": 200, "body": {"saved": {"id": 1},
                                                 "draft": None}}})
    kinds = {}
    for q in r:
        kinds[q] = kinds.get(q, 0) + 1
    assert set(kinds) == {"GET /api/state", "POST /api/ping"}, kinds
    assert kinds["GET /api/state"] == 1
    assert kinds["POST /api/ping"] >= 30      # 600 s / 20 s, and the first


def test_blink_theme_itself_prints_no_end_line(home, capsys):
    """`blink theme` says how it ended in its own words, in the terminal
    that is looking at it."""
    s = theme_server.BuilderServer(home)
    s.start()
    s.finish("closed")
    s.wait(5)
    assert "builder ended" not in capsys.readouterr().err
