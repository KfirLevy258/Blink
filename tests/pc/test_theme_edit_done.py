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
import threading
import time

from pc import protocol
from pc import theme
from pc import theme_server
from pc.bridge import Bridge

from tests.pc.test_theme_server import Clock, FakeServer, launcher_with
from tests.pc.test_theme_bridge import _NO_FEED


def inline(fn):
    fn()


def wired(tmp_path, launcher, caps=("svc", "theme", "edit_done")):
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
    if caps is not None:
        b.on_message({"t": "hello", "v": 2, "fw": "1.4.0",
                      "caps": list(caps)})
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


# ------------------------------------------------------------- review r2

def test_pump_never_waits_for_a_browser_that_is_slow_to_open(tmp_path):
    """MUST-1 of the second review: ended() took the Launcher's lock, which
    launch() holds across the opener, so every tap stalled the serial loop
    until the browser had started -- 1.90 s measured with a 2 s opener, and
    an opener has been seen stuck for 28.7 s, past the board's 30 s HOST
    LOST."""
    entered, release = threading.Event(), threading.Event()

    def slow(url):
        entered.set()
        release.wait(5)
        return True

    lau, _ = launcher_with(str(tmp_path), slow)
    b, sent = wired(tmp_path, lau)
    t = threading.Thread(target=lau.launch)
    t.start()
    try:
        assert entered.wait(2)
        t0 = time.monotonic()
        b.pump()
        assert lau.ended() is False
        assert time.monotonic() - t0 < 0.2
    finally:
        release.set()
        t.join(5)


def test_a_reshow_holds_off_the_builders_own_end(tmp_path):
    # A tap is an intent to edit: the running builder is touched before
    # its link is opened, so a tab closed a moment ago, or an idle limit
    # about to run out, cannot end it under the new tab.
    lau, clock = launcher_with(str(tmp_path), lambda u: True)
    lau.launch()
    clock.t += theme_server.OPEN_EVERY_S
    lau.launch()
    assert len(FakeServer.made) == 1
    assert FakeServer.made[0].touches == 1


def test_a_builder_that_ends_while_the_browser_opens_is_replaced(tmp_path):
    # It finished inside the opener (its grace ran out). Telling the board
    # "opened" over it would put "Opened on your computer" over a builder
    # that is not there.
    lau, clock = launcher_with(str(tmp_path), lambda u: True)
    b, sent = wired(tmp_path, lau)
    b.on_message({"t": "theme_edit", "v": 2})
    first = FakeServer.made[0]

    def dying(url):
        if url.startswith("http://127.0.0.1:1/open?k=2"):
            first.finish("closed")
        return True

    lau._opener = dying
    clock.t += theme_server.OPEN_EVERY_S
    b.on_message({"t": "theme_edit", "v": 2})
    assert len(FakeServer.made) == 2          # a builder of its own
    assert edit_lines(sent)[-1] == OK
    b.pump()
    assert DONE not in edit_lines(sent)       # the new one is still up
    FakeServer.made[1].finish("saved")
    b.pump()
    assert edit_lines(sent)[-1] == DONE


def test_no_done_to_a_theme_board_without_the_handler(tmp_path):
    # 1.4.0 builds before the handler say "theme" but not "edit_done", and
    # read a done during their own ask as an ok 0.
    lau, _ = launcher_with(str(tmp_path), lambda u: True)
    b, sent = wired(tmp_path, lau, caps=("svc", "theme"))
    b.on_message({"t": "theme_edit", "v": 2})
    FakeServer.made[0].finish("closed")
    b.pump()
    assert edit_lines(sent) == [OK]


def test_a_done_waits_for_a_board_that_has_not_spoken_yet(tmp_path):
    lau, _ = launcher_with(str(tmp_path), lambda u: True)
    lau.launch()
    FakeServer.made[0].finish("closed")
    b, sent = wired(tmp_path, lau, caps=None)  # a fresh connection
    b.pump()
    assert DONE not in sent                    # not used up on nobody
    b.on_message({"t": "hello", "v": 2, "fw": "1.4.0",
                  "caps": ["svc", "theme", "edit_done"]})
    b.pump()
    assert edit_lines(sent)[-1] == DONE


def test_only_the_newest_unspent_link_opens_and_only_while_fresh(tmp_path):
    s, clock = server(tmp_path)
    try:
        old = s.new_link().split("k=", 1)[1]
        new = s.new_link().split("k=", 1)[1]
        assert s.redeem(old) is None           # superseded
        assert s.redeem(new) is not None
        assert s.redeem(new) is None           # spent
        late = s.new_link().split("k=", 1)[1]
        clock.t += theme_server.LINK_LIFE_S
        assert s.redeem(late) is None          # stale
    finally:
        s.finish("test over")


def test_a_press_after_the_board_gives_its_button_back_opens_again():
    """The board takes a tap at any time except while its ask is in flight,
    and shows "Opened on your computer" for EDIT_OPENED_MS before reading
    its name again (owner, 2026-10-29). The Launcher folds an ask within
    OPEN_EVERY_S of the open into it, with no new window -- a press on the
    "Opened" note included, which is benign. What must hold is that a press
    after the note has gone always opens again: the board's 3 s starts at
    the reply and the daemon's 2 s before the opener runs, so the daemon's
    window must be the shorter."""
    import re
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "firmware" / "src"
           / "ui_settings.c").read_text(encoding="utf-8")
    m = re.search(r"#define EDIT_OPENED_MS\s+(\d+)\s*$", src, re.M)
    assert m, "EDIT_OPENED_MS not found as a plain integer in ui_settings.c"
    opened_ms = int(m.group(1))
    assert theme_server.OPEN_EVERY_S * 1000 < opened_ms
    assert "EDIT_COOLDOWN_MS" not in src
