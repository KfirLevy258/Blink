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
import os
import queue
import threading
import time

import pytest

from pc import protocol
from pc import theme
from pc import theme_server
from pc.bridge import Bridge

from tests.pc.test_theme_server import Clock, FakeServer, launcher_with
from tests.pc.test_theme_bridge import _NO_FEED
from tests.support import builder_page


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


# ------------------------------------------------------------- one live tab

def test_a_newer_tab_supersedes_the_older_ones(tmp_path):
    """Review of fp/edit-reset, SHOULD-1: with the board's button no longer
    locked out, each press while the builder was open added a tab, and on
    Windows they pile up behind other windows. The newest tab is the live
    one; an older tab is told on its next ping and stops."""
    s, clock = server(tmp_path)
    try:
        assert s.seen("a") is False
        assert s.seen("b") is False           # a press opened another
        assert s.seen("a") is True            # a's next ping: stop
        assert s.seen("a") is True            # and it stays stopped
        assert s.seen("b") is False
        assert s.seen() is False              # an id-less ping is never
    finally:                                  # told to stop
        s.finish("test over")


def test_a_superseded_tab_keeps_nothing_alive(tmp_path):
    # Its ping must not count as the page being seen, or a stopped tab
    # left open would hold the builder up after the live one closed.
    s, clock = server(tmp_path)
    try:
        s.seen("a")
        s.seen("b")
        assert s.seen("a") is True
        s.closing("b")
        s.seen("a")                           # the old tab pings again
        clock.t += theme_server.CLOSE_GRACE_S + 1
        s.tick()
        assert s.outcome == "closed"
    finally:
        s.finish("test over")


def test_a_tab_back_from_sleep_does_not_take_over_from_the_newest(tmp_path):
    # Order is by first sight, not last ping: a tab pruned after
    # PAGE_ALIVE_S that pings again is still the older one.
    s, clock = server(tmp_path)
    try:
        s.seen("a")
        s.seen("b")
        clock.t += theme_server.PAGE_ALIVE_S + 1
        s.seen("b")
        assert s.seen("a") is True
        assert s.seen("b") is False
    finally:
        s.finish("test over")


def test_the_ping_answer_tells_an_older_tab_over_http(tmp_path):
    from tests.pc.test_theme_server import login, request
    s, clock = server(tmp_path)
    s.start()
    try:
        cookie = login(s)
        ping = lambda tab: json.loads(request(
            s, "POST", "/api/ping", {"tab": tab}, cookie=cookie)[2])
        assert ping("old1") == {"ok": True, "newer": False}
        assert ping("new2") == {"ok": True, "newer": False}
        assert ping("old1") == {"ok": True, "newer": True}
    finally:
        s.finish("test over")
        s.wait(5)


# The page half, run as a browser would run it (tests/support/builder_dom.js):
# the tests that stood here matched strings in the script, and the review
# of fp/edit-reset r2 found what that let through.
SAVED_DARK = {"/api/state": {"status": 200,
                             "body": {"saved": {"id": 0}, "draft": None}}}


def _newer_on_ping(extra=""):
    return f"""
    answer('/api/ping', () => ({{ status: 200, body: {{ ok: true, newer: true }} }}));
    {extra}
    await advance(20000);
    """


def test_an_older_tab_with_nothing_unsaved_closes_itself():
    r = builder_page.run("""
    await advance(10);
    """ + _newer_on_ping() + """
    return { closed: window.closed, status: $('status-text').textContent,
             save: $('save').disabled };
    """, SAVED_DARK)
    assert r == {"closed": 1, "save": True,
                 "status": "Continued in a newer tab. You can close this one."}


def test_an_older_tab_with_a_draft_never_closes_itself():
    """R2-SHOULD-1: Chrome, Edge and Firefox let a script close a tab with
    one history entry, which a builder tab opened from the board is. So the
    older tab closed itself, draft and all, without a word. It stays now,
    still, with its colours, and its notice offers the share code; the
    copy buttons behind the notice are as still as the rest."""
    r = builder_page.run("""
    await advance(10);
    preset('Ocean');
    await advance(300);
    """ + _newer_on_ping() + """
    return { closed: window.closed, status: $('status-text').textContent,
             ocean: $('presets').querySelectorAll('button')
                      .filter(b => b.getAttribute('aria-pressed') === 'true')
                      .map(b => b.querySelector('.pn').textContent),
             copy: $('copy-code').disabled, copyCmd: $('copy-cmd').disabled,
             offer: $('notice-copy').getAttribute('hidden') === null && !$('notice-copy').disabled };
    """, SAVED_DARK)
    assert r["closed"] == 0
    assert r["status"].startswith("Continued in a newer tab. Your changes")
    assert r["ocean"] == ["Ocean"]
    assert r["copy"] is True and r["copyCmd"] is True
    assert r["offer"] is True


def test_a_superseded_tab_goes_still_and_keeps_its_message():
    """R2-NICE 1 and 2: a change in the 250 ms before the answer still sent
    a preview after the tab stopped, and the controls stayed live under a
    status any later say() could overwrite."""
    r = builder_page.run("""
    await advance(10);
    answer('/api/ping', () => ({ status: 200, body: { ok: true, newer: true } }));
    await advance(19900);
    preset('Ocean');                 // its preview is due at +250 ms
    await advance(100);              // the ping lands first: superseded
    const said = $('status-text').textContent;
    await advance(1000);
    $('paste').value = 'nonsense';
    $('load').fire('click');         // disabled; and a message would go
    return {                         // under the box, not over the bar
      previews: requests.filter(q => q.path === '/api/preview').length,
      said, now: $('status-text').textContent,
      live: document.querySelectorAll('main button, main input')
              .filter(e => !e.disabled && e.id !== 'copy-cmd').map(e => e.id),
    };
    """, SAVED_DARK)
    assert r["previews"] == 0
    assert r["said"].startswith("Continued in a newer tab.")
    assert r["now"] == r["said"]
    assert r["live"] == []


@pytest.mark.parametrize("path, button", [("/api/save", "save"),
                                          ("/api/cancel", "cancel")])
def test_a_refused_save_or_cancel_stops_the_older_tab(path, button):
    """R2-SHOULD-2, the page half: every write carries the tab id, and a
    409 "newer" stops the tab as a ping would have, keeping its draft."""
    r = builder_page.run(f"""
    await advance(10);
    preset('Ocean');
    await advance(300);
    answer('{path}', () => ({{ status: 409, body: {{ newer: true }} }}));
    $('{button}').click();
    await advance(10);
    return {{ closed: window.closed, status: $('status-text').textContent,
             tabs: requests.filter(q => q.method === 'POST' && q.path !== '/api/closed')
                     .map(q => q.headers['X-Blink-Tab'] || null) }};
    """, SAVED_DARK)
    assert r["closed"] == 0
    assert r["status"].startswith("Continued in a newer tab.")
    assert all(r["tabs"]) and len(set(r["tabs"])) == 1


def test_a_tab_coming_back_into_view_asks_at_once():
    r = builder_page.run("""
    await advance(10);
    const before = requests.filter(q => q.path === '/api/ping').length;
    document.hidden = true; document.fire('visibilitychange');
    const hidden = requests.filter(q => q.path === '/api/ping').length;
    document.hidden = false; document.fire('visibilitychange');
    return [before, hidden, requests.filter(q => q.path === '/api/ping').length];
    """, SAVED_DARK)
    assert r == [1, 1, 2]


def test_a_newer_tab_opens_on_the_draft():
    """R2-SHOULD-1, option (b): /api/state carries the last preview, and
    the newer tab starts from it rather than from the saved theme."""
    r = builder_page.run("""
    await advance(300);
    return { status: $('status-text').textContent,
             pressed: $('presets').querySelectorAll('button')
                        .filter(b => b.getAttribute('aria-pressed') === 'true')
                        .map(b => b.querySelector('.pn').textContent),
             previews: requests.filter(q => q.path === '/api/preview').length };
    """, {"/api/state": {"status": 200, "body": {"saved": {"id": 0},
                                                 "draft": {"id": 1}}}})
    names = [b["name"] for b in __import__(
        "json").loads(theme_server.builtins_json())]
    assert r["pressed"] == [names[1]]
    assert r["status"].startswith("This picks up the changes")
    assert r["previews"] == 0         # the board already shows it


def test_the_server_refuses_a_superseded_tabs_write(tmp_path):
    """R2-SHOULD-2, the server half: before the older tab's next ping, its
    Save or Cancel ended the builder under the newer tab."""
    from tests.pc.test_theme_server import login, request
    s, clock = server(tmp_path)
    s.start()
    try:
        cookie = login(s)
        s.seen("old1")
        s.seen("new2")
        tab = lambda t: {"X-Blink-Tab": t}
        for path in ("/api/preview", "/api/save", "/api/cancel"):
            status, _, body = request(s, "POST", path, {"id": 3},
                                      cookie=cookie, extra=tab("old1"))
            assert status == 409, path
            assert json.loads(body)["newer"] is True
        assert s.outcome is None
        assert not os.path.exists(theme.preview_path(s.home))
        status, _, _ = request(s, "POST", "/api/preview", {"id": 3},
                               cookie=cookie, extra=tab("new2"))
        assert status == 200
        # An id not yet heard is a page that has just loaded: the newest.
        status, _, _ = request(s, "POST", "/api/preview", {"id": 4},
                               cookie=cookie, extra=tab("fresh3"))
        assert status == 200
        assert s.outcome is None
    finally:
        s.finish("test over")
        s.wait(5)


def test_state_carries_the_last_preview_as_the_draft(tmp_path):
    from tests.pc.test_theme_server import login, request
    s, clock = server(tmp_path)
    s.start()
    try:
        cookie = login(s)
        state = lambda: json.loads(request(s, "GET", "/api/state",
                                           cookie=cookie)[2])
        assert state()["draft"] is None
        request(s, "POST", "/api/preview", {"id": 5}, cookie=cookie)
        assert state()["draft"] == {"id": 5}
    finally:
        s.finish("test over")
        s.wait(5)


def test_the_done_is_logged_once_for_a_desk_check(tmp_path, capsys):
    # windows-check 10c reads it: the board no longer shows a done, since
    # the slot comes back to its name by itself.
    lau, _ = launcher_with(str(tmp_path), lambda u: True)
    b, sent = wired(tmp_path, lau)
    b.on_message({"t": "theme_edit", "v": 2})
    capsys.readouterr()
    FakeServer.made[0].finish("closed")
    b.pump()
    b.pump()
    err = capsys.readouterr().err
    assert err.count("[theme] the builder ended; told the board") == 1
