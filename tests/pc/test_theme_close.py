"""A builder tab that saved or cancelled closes itself, and a tab the owner
closes ends the builder without a long wait.

The owner, 2026-10-31: "can we just close the tab after like 5 sec?" and "i
closed the tab on the computer, but the board took like 5 seconds to return
to edit on computer". The page half runs in the Node harness on a fake
clock; the server half drives BuilderServer's own clock. Nothing here opens a
browser, so what a browser does with window.close() is argued from the page
and the redirect, not measured: see the comment on closeSoon in the page.
"""
import re
import urllib.parse

import pytest

from pc import theme_server
from tests.support import builder_page

from tests.pc.test_theme_server import (  # noqa: F401
    Clock, home, login, request, server)

SAVED_DARK = {"/api/state": {"status": 200,
                             "body": {"saved": {"id": 0}, "draft": None}}}
_OCEAN = "preset('Ocean'); await advance(300);"
_SAVE = "$('save').click(); await advance(10);"
_CANCEL = "$('cancel').click(); await advance(10);"
_BAR = "$('status-text').textContent"
_SAVED = "Saved: Dark. Your board keeps it."
_CANCELLED = "Cancelled. Your board is back on its saved theme."


@pytest.mark.parametrize("press, base", [(_SAVE, _SAVED),
                                         (_CANCEL, _CANCELLED)],
                         ids=["save", "cancel"])
def test_a_clean_tab_counts_down_and_closes(press, base):
    r = builder_page.run(f"""
    await advance(10);
    {press}
    const at = [{_BAR}];
    for (let i = 0; i < 4; i++) {{ await advance(1000); at.push({_BAR}); }}
    const before = window.calls;
    await advance(1000);
    const live = $('status').getAttribute('aria-live');
    await advance(5000);
    return {{ at, before, calls: window.calls, closed: window.closed,
             after: {_BAR}, live }};
    """, SAVED_DARK)
    assert r["at"] == [f"{base} This tab closes in {n} s." for n in
                       (5, 4, 3, 2, 1)]
    assert r["before"] == 0         # not a second early
    assert r["calls"] == 1 and r["closed"] == 1
    # The browser took it, so the line is left as it was: nothing says
    # "close it yourself" over a tab that is going.
    assert r["after"] == f"{base} This tab closes in 1 s."


def test_a_tab_the_browser_refuses_to_close_says_so_after_a_moment():
    r = builder_page.run(f"""
    await advance(10);
    window.refuse = true;
    {_SAVE}
    await advance(5000);
    const asked = [window.calls, {_BAR}];
    const live = $('status').getAttribute('aria-live');
    await advance(700);
    return {{ asked, now: {_BAR}, live: [live, $('status').getAttribute('aria-live')],
             calls: window.calls }};
    """, SAVED_DARK)
    assert r["asked"] == [1, f"{_SAVED} This tab closes in 1 s."]
    assert r["now"] == f"{_SAVED} You can close this tab."
    # Off for the ticks, so a screen reader is not read a line a second,
    # and back on for the line that ends them.
    assert r["live"] == ["off", "polite"]
    assert r["calls"] == 1          # asked once, not in a loop


@pytest.mark.parametrize("event", [
    "document.fire('keydown', { key: 'a' })",
    "document.fire('mousedown', { clientX: 5, clientY: 5 })",
    "document.fire('touchstart', {})",
], ids=["key", "click", "touch"])
def test_the_owner_stops_the_countdown_and_keeps_the_tab(event):
    r = builder_page.run(f"""
    await advance(10);
    {_SAVE}
    await advance(2000);
    {event};
    const said = {_BAR};
    await advance(20000);
    return {{ said, now: {_BAR}, calls: window.calls,
             live: $('status').getAttribute('aria-live') }};
    """, SAVED_DARK)
    assert r["said"] == f"{_SAVED} You can close this tab."
    assert r["now"] == r["said"]
    assert r["calls"] == 0          # never closed under somebody reading it
    assert r["live"] == "polite"


def test_moving_the_mouse_or_the_wheel_does_not_stop_the_countdown():
    """A hand moves the mouse away from Save as a matter of course. The
    first version stopped the countdown for 12 px of it, and on the owner's
    desk (2026-10-31, real Chrome) no saved tab ever closed."""
    r = builder_page.run(f"""
    await advance(10);
    {_SAVE}
    document.fire('mousemove', {{ clientX: 100, clientY: 100 }});
    document.fire('mousemove', {{ clientX: 900, clientY: 700 }});
    document.fire('wheel', {{ deltaY: 40 }});
    await advance(5100);
    return {{ calls: window.calls, closed: window.closed }};
    """, SAVED_DARK)
    assert r == {"calls": 1, "closed": 1}


def test_input_after_the_tab_closed_or_before_a_save_does_nothing():
    r = builder_page.run(f"""
    await advance(10);
    document.fire('keydown', {{ key: 'a' }});
    document.fire('mousemove', {{ clientX: 1, clientY: 1 }});
    document.fire('mousemove', {{ clientX: 900, clientY: 900 }});
    const live = {_BAR};
    {_SAVE}
    return {{ live, now: {_BAR} }};
    """, SAVED_DARK)
    assert r["now"] == f"{_SAVED} This tab closes in 5 s."


def test_a_tab_with_changes_made_after_saving_is_not_closed():
    r = builder_page.run(f"""
    await advance(10);
    {_OCEAN}
    const s = hold();
    answer('/api/save', () => s.p);
    {_SAVE}
    pick('c-high', '#ff0000'); await advance(10);
    s.release({{ status: 200, body: {{ ok: true }} }}); await advance(10);
    await advance(30000);
    return {{ said: {_BAR}, calls: window.calls,
             copy: $('copy-code').disabled }};
    """, SAVED_DARK)
    assert r["said"].startswith("Saved: Ocean. Your board keeps it. Changes"
                                " you made after saving")
    assert "closes in" not in r["said"]
    assert r["calls"] == 0 and r["copy"] is False


@pytest.mark.parametrize("ending", [
    "answer('/api/ping', () => 'fail'); await advance(23100);",
    "answer('/api/save', () => ({ status: 409, body: {} })); " + _SAVE,
], ids=["stopped", "superseded by a refused save"])
def test_other_endings_never_count_down(ending):
    r = builder_page.run(f"""
    await advance(10);
    {ending}
    await advance(30000);
    return {{ said: {_BAR} }};
    """, SAVED_DARK)
    assert "closes in" not in r["said"]
    assert "This tab closes" not in r["said"]


def test_a_cancel_that_never_reached_the_builder_does_not_count_down():
    r = builder_page.run(f"""
    await advance(10);
    answer('/api/cancel', () => 'fail');
    {_CANCEL}
    await advance(30000);
    return {{ said: {_BAR}, calls: window.calls }};
    """, SAVED_DARK)
    assert r["said"].startswith("The builder has stopped.")
    assert r["calls"] == 0


# ----------------------------------------- what makes the close allowed

def test_the_link_opens_the_builder_with_one_redirect_and_no_history(server):
    """A script may close a top-level tab whose session history holds one
    document. The board's link and the one `blink theme` prints are both
    new_link(): /open?k= answers 303 to /, a redirect adds no entry, and /
    answers the page itself. Nothing in the page pushes or replaces
    history, or navigates, so the entry count stays at one."""
    link = urllib.parse.urlsplit(server.new_link())
    status, headers, _ = request(server, "GET", link.path + "?" + link.query)
    assert status == 303 and headers["Location"] == "/"
    cookie = headers["Set-Cookie"].split(";", 1)[0]
    status, headers, body = request(server, "GET", "/", cookie=cookie)
    assert status == 200 and "Location" not in headers
    _, script = builder_page.page_parts()
    for nav in ("location.", "history.", "pushState", "replaceState",
                "window.open", "<meta http-equiv", "target="):
        assert nav not in script and nav not in body.decode(), nav


# ---------------------------------------------- closing the tab, quickly

def _close_at(server, cookie, after):
    request(server, "POST", "/api/closed", {"tab": "a"}, cookie=cookie)
    server.clock.t += after
    server.tick()


def test_a_closed_tab_ends_the_builder_after_the_short_grace(server):
    cookie = login(server)
    request(server, "POST", "/api/ping", {"tab": "a"}, cookie=cookie)
    server.clock.t += 60        # a page that has been open a while
    _close_at(server, cookie, theme_server.CLOSE_GRACE_S - 0.1)
    assert not server.done
    server.clock.t += 0.2
    server.tick()
    assert server.wait(5) == "closed"
    assert theme_server.CLOSE_GRACE_S <= 2


def test_a_reload_is_not_a_close_when_the_page_comes_back_in_time(server):
    """A reload serves the page (GET /) before the old one is unloaded, so
    the close that follows is within RELOAD_WINDOW_S of it."""
    cookie = login(server)
    request(server, "POST", "/api/ping", {"tab": "a"}, cookie=cookie)
    server.clock.t += 60
    request(server, "GET", "/", cookie=cookie)              # the reload
    server.clock.t += 0.05
    request(server, "POST", "/api/closed", {"tab": "a"}, cookie=cookie)
    for _ in range(4):          # well past the short grace
        server.clock.t += 1
        server.tick()
    assert not server.done
    request(server, "POST", "/api/ping", {"tab": "b"}, cookie=cookie)
    server.clock.t += 60
    server.tick()
    assert not server.done      # the new page's ping took the close back


def test_a_slow_reload_gets_the_old_grace_and_then_no_more(server):
    cookie = login(server)
    request(server, "GET", "/", cookie=cookie)
    server.clock.t += 0.05
    request(server, "POST", "/api/closed", {"tab": "a"}, cookie=cookie)
    server.clock.t += theme_server.RELOAD_GRACE_S - 0.1
    server.tick()
    assert not server.done
    server.clock.t += 0.2
    server.tick()
    assert server.wait(5) == "closed"


def test_a_page_served_after_the_close_takes_it_back_at_once(server):
    """A browser that unloads before it asks: the GET / that follows is a
    touch, which clears the close, however short the grace is."""
    cookie = login(server)
    request(server, "POST", "/api/ping", {"tab": "a"}, cookie=cookie)
    server.clock.t += 60
    request(server, "POST", "/api/closed", {"tab": "a"}, cookie=cookie)
    server.clock.t += theme_server.CLOSE_GRACE_S - 0.5
    request(server, "GET", "/", cookie=cookie)
    server.clock.t += 10
    server.tick()
    assert not server.done


def test_the_deadlines_are_checked_often_enough_to_keep_the_grace():
    """At a 1 s tick a 1.5 s grace could end after 2.5 s; the old 5 s one
    ended after 5 to 6 s, which is what the owner saw."""
    assert theme_server.TICK_S <= 0.25
    assert theme_server.CLOSE_GRACE_S + theme_server.TICK_S <= 2
