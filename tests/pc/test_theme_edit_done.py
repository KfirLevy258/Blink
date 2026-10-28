"""The board hears when the builder it asked for has ended (theme contract
Addendum 6).

Found on the board on 2026-10-29: "Edit on computer" opened the builder, the
owner closed the browser window, and a second tap inside the board's 10 s
cooldown sent nothing while the slot went on saying "Opened on your
computer". Only leaving the theme page and coming back cleared it. Two
halves fix it, and both are pinned here:

  - the daemon sends {"t":"theme_edit","done":1} whenever a builder it
    opened for the board ends, which the board takes as "ask again";
  - a theme_edit while that builder is still running makes a browser show
    it again, on a fresh one-time link, rather than an ok with no window.

Every line the Bridge writes here goes through protocol.encode_checked,
as the daemon's writer does (CLAUDE.md), so a done that outgrew the
board's line budget would fail here rather than vanish on the wire.
"""
import json
import queue

from pc import protocol
from pc import theme
from pc import theme_server
from pc.bridge import Bridge

from tests.pc.test_theme_server import Clock, FakeServer, launcher_with
from tests.pc.test_theme_bridge import _NO_FEED


def inline(fn):
    fn()


def wired(tmp_path, launcher, caps=("svc", "theme")):
    """A Bridge wired to `launcher` the way claude_usage_bridge does it,
    writing through encode_checked."""
    home = str(tmp_path / ".blink")
    sent = []

    def write(m):
        raw, why = protocol.encode_checked(m)
        assert raw is not None, why
        sent.append(json.loads(raw))
        return True

    b = Bridge(write_msg=write, fetch_usage=lambda: None, now=Clock(),
               wall=lambda: (1752444000, 0), theme_sync=theme.ThemeSync(home),
               open_builder=launcher.launch, builder_ended=launcher.ended,
               run_async=inline, edit_answers=queue.Queue(), **_NO_FEED)
    b.on_message({"t": "hello", "v": 2, "fw": "1.4.0", "caps": list(caps)})
    return b, sent


def edit_lines(sent):
    return [m for m in sent if m["t"] == "theme_edit"]


DONE = {"t": "theme_edit", "v": protocol.VERSION, "done": 1}
OK = {"t": "theme_edit", "v": protocol.VERSION, "ok": 1}


def test_the_board_is_told_once_when_the_builder_it_opened_ends(tmp_path):
    lau, clock = launcher_with(str(tmp_path), lambda u: True)
    b, sent = wired(tmp_path, lau)
    b.on_message({"t": "theme_edit", "v": 2})
    assert edit_lines(sent) == [OK]
    b.pump()
    assert edit_lines(sent) == [OK]          # still running: nothing yet
    FakeServer.made[0].finish("closed")       # the tab was closed
    b.pump()
    b.pump()
    assert edit_lines(sent) == [OK, DONE]     # said once


def test_the_owners_case_close_then_tap_again_opens_again(tmp_path):
    """The board run: open, close the window, tap again before 10 s."""
    opened = []
    lau, clock = launcher_with(str(tmp_path), lambda u: opened.append(u)
                               or True)
    b, sent = wired(tmp_path, lau)
    b.on_message({"t": "theme_edit", "v": 2})
    clock.t += 4
    FakeServer.made[0].finish("closed")
    b.pump()                                  # done reaches the board
    clock.t += 1                              # 5 s after the first ask
    b.on_message({"t": "theme_edit", "v": 2})
    assert edit_lines(sent) == [OK, DONE, OK]
    assert len(FakeServer.made) == 2 and len(opened) == 2


def test_an_ask_while_the_builder_is_open_shows_it_again(tmp_path):
    opened = []
    lau, clock = launcher_with(str(tmp_path), lambda u: opened.append(u)
                               or True)
    b, sent = wired(tmp_path, lau)
    b.on_message({"t": "theme_edit", "v": 2})
    FakeServer.made[0].alive = True           # the page is open in a tab
    clock.t += theme_server.OPEN_EVERY_S
    b.on_message({"t": "theme_edit", "v": 2})
    # A fresh one-time link on the same builder, not a silent ok.
    assert len(FakeServer.made) == 1
    assert opened == ["http://127.0.0.1:1/open?k=1",
                      "http://127.0.0.1:1/open?k=2"]
    assert edit_lines(sent) == [OK, OK]


def test_no_done_for_a_builder_that_never_showed(tmp_path):
    # No browser: the board shows its own "run blink theme" fallback, and a
    # done would wipe it straight off the slot.
    lau, _ = launcher_with(str(tmp_path), lambda u: False)
    b, sent = wired(tmp_path, lau)
    b.on_message({"t": "theme_edit", "v": 2})
    b.pump()
    assert DONE not in edit_lines(sent)
    assert FakeServer.made[0].done


def test_no_done_to_a_board_that_takes_no_themes(tmp_path):
    lau, _ = launcher_with(str(tmp_path), lambda u: True)
    b, sent = wired(tmp_path, lau, caps=("svc",))
    lau.launch()                              # opened some other way
    FakeServer.made[0].finish("saved")
    b.pump()
    assert DONE not in sent


def test_the_done_line_fits_the_board_and_says_nothing_else():
    raw, why = protocol.encode_checked(protocol.theme_edit_done())
    assert raw is not None, why
    assert json.loads(raw) == DONE


# ------------------------------------------------------------- two tabs

def server(tmp_path):
    clock = Clock()
    return theme_server.BuilderServer(str(tmp_path / ".blink"),
                                      now=clock), clock


def test_closing_the_older_tab_does_not_end_the_builder_in_the_newer(
        tmp_path):
    s, clock = server(tmp_path)
    try:
        s.seen("a")
        s.seen("b")                           # opened by a second ask
        s.closing("a")
        clock.t += theme_server.CLOSE_GRACE_S + 1
        s.tick()
        assert not s.done and s.page_alive()
        s.closing("b")
        clock.t += theme_server.CLOSE_GRACE_S + 1
        s.tick()
        assert s.outcome == "closed"
    finally:
        s.finish("test over")


def test_a_tab_gone_quiet_does_not_hold_the_builder_open(tmp_path):
    # A tab that crashed never says it closed; past PAGE_ALIVE_S it no
    # longer counts, so the last tab's close still ends the builder.
    s, clock = server(tmp_path)
    try:
        s.seen("crashed")
        clock.t += theme_server.PAGE_ALIVE_S + 1
        s.seen("live")
        s.closing("live")
        clock.t += theme_server.CLOSE_GRACE_S + 1
        s.tick()
        assert s.outcome == "closed"
    finally:
        s.finish("test over")


def test_a_tab_id_is_taken_only_when_short_and_plain():
    assert theme_server._tab_of(b'{"tab":"ab12cd34"}') == "ab12cd34"
    for raw in (b"", b"nonsense", b'{"tab":"' + b"a" * 40 + b'"}',
                b'{"tab":"a b"}', b'{"tab":7}', b"[1]"):
        assert theme_server._tab_of(raw) is None, raw


def test_the_page_names_its_tab_on_ping_and_close():
    page = theme_server.page()
    assert "post('/api/ping', { tab: TAB })" in page
    assert "body: JSON.stringify({ tab: TAB })" in page
    assert "ping();" in page
