"""The daemon's half of holding "Edit on computer" while its builder is open.

Owner, 2026-10-30, after a board run: "the edit on computer button should
re-appear ... only once the page is not active / closed". The board now
holds its slot from an ok 1 until theme_edit done:1 (firmware/src/
edit_hold.h, host-tested in tests/edit_hold). A hold the daemon never ends
is a button that never comes back, so the daemon promises two things, and
says so with the welcome cap "edit_done":

  - done:1 when the builder it opened ends (Addendum 6, already sent);
  - "editing" in every welcome greet() writes: whether that builder is open
    right now. It is how a board that rebooted, was unplugged, or sat under
    a daemon that restarted learns to hold again or to let go.

Everything the Bridge writes here goes through protocol.encode_checked, as
the daemon's writer does (CLAUDE.md).
"""
import json
import queue
import re
import threading
import time
from pathlib import Path

from pc import protocol
from pc import theme
from pc import theme_server
from pc.bridge import Bridge

from tests.pc.test_theme_server import Clock, FakeServer, launcher_with
from tests.pc.test_theme_bridge import _NO_FEED

ROOT = Path(__file__).resolve().parents[2]


def inline(fn):
    fn()


def wired(tmp_path, launcher, builder_open="launcher", clock=None,
          run_async=inline, open_builder=None):
    home = str(tmp_path / ".blink")
    sent = []

    def write(m):
        raw, why = protocol.encode_checked(m)
        assert raw is not None, why
        sent.append(json.loads(raw))
        return True

    if builder_open == "launcher":
        builder_open = launcher.editing
    b = Bridge(write_msg=write, fetch_usage=lambda: None,
               now=clock or Clock(), wall=lambda: (1752444000, 0),
               theme_sync=theme.ThemeSync(home),
               open_builder=open_builder or launcher.launch,
               builder_ended=launcher.ended,
               builder_open=builder_open, run_async=run_async,
               edit_answers=queue.Queue(), **_NO_FEED)
    return b, sent


def hello(b):
    b.on_message({"t": "hello", "v": 2, "fw": "1.4.0",
                  "caps": ["svc", "theme", "edit_done"]})


def welcomes(sent):
    return [m for m in sent if m["t"] == "welcome"]


def test_the_welcome_promises_the_done():
    w = protocol.welcome("blink-bridge", "1.4.0")
    assert "edit_done" in w["caps"]
    # The board finds a cap as a whole member of this list wherever it is
    # (theme_caps_has walks them all), so only "a list of plain strings"
    # matters, and "theme" is still in it for the boards before the hold.
    assert all(isinstance(c, str) for c in w["caps"])
    assert "theme" in w["caps"]
    # The probe's welcome cannot say, and says nothing.
    assert "editing" not in w


def test_greet_says_no_builder_is_open(tmp_path):
    lau, _ = launcher_with(str(tmp_path), lambda u: True)
    b, sent = wired(tmp_path, lau)
    hello(b)
    assert welcomes(sent)[-1]["editing"] == 0


def test_a_board_back_while_the_builder_is_open_is_told_so(tmp_path):
    """The board rebooted (or its cable was out) after its ask opened the
    builder: its hold is gone, the builder is not. The hello's welcome puts
    the hold back."""
    lau, _ = launcher_with(str(tmp_path), lambda u: True)
    b, sent = wired(tmp_path, lau)
    hello(b)
    b.on_message({"t": "theme_edit", "v": 2})
    assert {"t": "theme_edit", "v": protocol.VERSION, "ok": 1} in sent
    # A new connection, as the daemon makes after a reconnect.
    b2, sent2 = wired(tmp_path, lau)
    hello(b2)
    assert welcomes(sent2)[-1]["editing"] == 1


def test_a_builder_that_ended_while_the_board_was_away_frees_it(tmp_path):
    lau, _ = launcher_with(str(tmp_path), lambda u: True)
    b, sent = wired(tmp_path, lau)
    hello(b)
    b.on_message({"t": "theme_edit", "v": 2})
    FakeServer.made[0].finish("closed")       # while the board is away
    b2, sent2 = wired(tmp_path, lau)
    hello(b2)
    assert welcomes(sent2)[-1]["editing"] == 0
    # And the done still follows once, harmless to a board already free.
    b2.pump()
    assert {"t": "theme_edit", "v": protocol.VERSION, "done": 1} in sent2


def test_after_the_done_the_welcome_says_shut(tmp_path):
    lau, _ = launcher_with(str(tmp_path), lambda u: True)
    b, sent = wired(tmp_path, lau)
    hello(b)
    b.on_message({"t": "theme_edit", "v": 2})
    FakeServer.made[0].finish("saved")
    b.pump()
    hello(b)
    assert welcomes(sent)[-1]["editing"] == 0


def test_no_browser_is_no_builder(tmp_path):
    # ok 0: the board shows its fallback and holds nothing; a welcome after
    # must not hold it either.
    lau, _ = launcher_with(str(tmp_path), lambda u: False)
    b, sent = wired(tmp_path, lau)
    hello(b)
    b.on_message({"t": "theme_edit", "v": 2})
    hello(b)
    assert welcomes(sent)[-1]["editing"] == 0


def test_a_builder_the_board_did_not_ask_for_holds_nothing(tmp_path):
    """`blink theme` runs a builder of its own, outside the Launcher; its end
    is never told to the board, so it must never be reported as open."""
    lau, _ = launcher_with(str(tmp_path), lambda u: True)
    b, sent = wired(tmp_path, lau)
    hello(b)
    assert welcomes(sent)[-1]["editing"] == 0
    assert lau.editing() is False


def test_greet_never_waits_for_a_browser_that_is_slow_to_open(tmp_path):
    """greet() runs on the serial thread, and launch() holds the Launcher's
    lock across an opener measured stuck for 28.7 s. editing() answers None
    then, and the welcome goes without the field, which leaves the board as
    it is: the ok on its way holds it if the builder opens."""
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
        assert lau.editing() is None
        hello(b)
        assert "editing" not in welcomes(sent)[-1]
    finally:
        release.set()
        t.join(5)
    assert lau.editing() is True


def test_a_bridge_wired_without_it_says_nothing(tmp_path):
    lau, _ = launcher_with(str(tmp_path), lambda u: True)
    b, sent = wired(tmp_path, lau, builder_open=None)
    hello(b)
    assert "editing" not in welcomes(sent)[-1]


def test_a_failing_state_read_still_greets(tmp_path, capsys):
    lau, _ = launcher_with(str(tmp_path), lambda u: True)

    def broken():
        raise RuntimeError("boom")

    b, sent = wired(tmp_path, lau, builder_open=broken)
    hello(b)
    assert welcomes(sent) and "editing" not in welcomes(sent)[-1]


def test_the_widest_welcome_fits_the_board():
    raw, why = protocol.encode_checked(
        protocol.welcome("blink-bridge", "10.10.10", editing=True))
    assert raw is not None, why
    assert len(raw) < 160


def test_the_daemon_wires_the_state_into_its_bridge():
    # Read, never imported: importing the entry point has side effects.
    src = (ROOT / "claude_usage_bridge.py").read_text(encoding="utf-8")
    assert "builder_open=theme_builder.editing" in src


def _firmware(name):
    return (ROOT / "firmware" / "src" / name).read_text(encoding="utf-8")


def test_the_board_reads_the_cap_by_the_name_the_daemon_sends():
    assert '"edit_done"' in _firmware("edit_hold.c")
    assert '"editing"' in _firmware("edit_hold.c")
    assert "edit_done" in protocol.CAPS


def _hold_ms():
    src = _firmware("ui_settings.c")
    m = re.search(r"#ifndef EDIT_HOLD_MS\n#define EDIT_HOLD_MS \((\d+) \* "
                  r"(\d+)\)\n#endif", src)
    assert m, ("EDIT_HOLD_MS not found as #ifndef / (a * b) / #endif in "
               "ui_settings.c")
    return int(m.group(1)) * int(m.group(2))


def test_the_boards_bound_outlasts_two_restatements():
    """The board lets its hold go after EDIT_HOLD_MS with no restatement,
    ok 1 or preview. edit_state comes every EDIT_STATE_EVERY_S, so one lost
    line must not cost a builder that is open its hold -- two and a half,
    as svc lines have over SVC_HOLD_S -- and the bound must stay short
    enough that a hold over nothing (a done that overtook its ok) is gone
    in minutes, not the quarter of an hour it was."""
    hold_ms = _hold_ms()
    every_ms = protocol.EDIT_STATE_EVERY_S * 1000
    assert hold_ms >= 2.5 * every_ms
    assert hold_ms <= 5 * 60 * 1000
    # The serial loop's liveness window is shorter still, so a board heard
    # again after a silence is told at once, well inside it.
    from pc import bridge
    assert bridge.LIVENESS_WINDOW_S * 1000 < hold_ms


def test_the_bound_script_names_the_override_it_needs():
    # touch.sh hands BLINK_TOUCH_CFLAGS to the compiler; the script that
    # waits the bound out must say which value it was written for.
    script = (ROOT / "tools" / "bench" / "edit_hold_bound.txt").read_text(
        encoding="utf-8")
    assert "BLINK_TOUCH_CFLAGS=-DEDIT_HOLD_MS=4000" in script
    assert "harness-only" in script.splitlines()
    assert "${BLINK_TOUCH_CFLAGS:-}" in (
        ROOT / "tools" / "panel_render" / "touch.sh").read_text(
            encoding="utf-8")


# ------------------------------------------------------- the restatement

def alive(b, clock):
    hello(b)
    b.on_message({"t": "ping", "v": 2})


def states(sent):
    return [m["editing"] for m in sent if m["t"] == "edit_state"]


def test_the_state_is_restated_while_the_board_listens(tmp_path):
    clock = Clock()
    lau, _ = launcher_with(str(tmp_path), lambda u: True)
    b, sent = wired(tmp_path, lau, clock=clock)
    alive(b, clock)
    b.pump()
    assert states(sent) == [0]
    b.on_message({"t": "theme_edit", "v": 2})
    b.pump()
    assert states(sent) == [0]               # not before a minute is up
    clock.t += protocol.EDIT_STATE_EVERY_S
    b.on_message({"t": "ping", "v": 2})
    b.pump()
    assert states(sent) == [0, 1]
    FakeServer.made[0].finish("closed")
    clock.t += protocol.EDIT_STATE_EVERY_S
    b.on_message({"t": "ping", "v": 2})
    b.pump()
    # The done first, then the state that agrees with it.
    tail = [m for m in sent if m["t"] in ("theme_edit", "edit_state")][-2:]
    assert tail == [{"t": "theme_edit", "v": protocol.VERSION, "done": 1},
                    {"t": "edit_state", "v": protocol.VERSION, "editing": 0}]


def test_the_owners_lunch_holds_again_when_the_same_connection_wakes(
        tmp_path):
    """Review SHOULD-1, the reviewer's demo turned round: the Mac sleeps
    with the builder open, the board declares HOST LOST after 30 s and lets
    its hold go, and the same connection wakes. No welcome comes, but the
    first ping heard after the silence has the state said at once."""
    clock = Clock()
    lau, _ = launcher_with(str(tmp_path), lambda u: True)
    b, sent = wired(tmp_path, lau, clock=clock)
    alive(b, clock)
    b.on_message({"t": "theme_edit", "v": 2})
    b.pump()
    n = len(sent)
    clock.t += 45                            # asleep; the board is HOST LOST
    assert not b.board_alive()
    b.pump()
    assert sent[n:] == []                    # nothing into a silent port
    clock.t += 1
    b.on_message({"t": "ping", "v": 2})      # awake, same connection
    b.pump()
    assert [m for m in sent[n:] if m["t"] == "edit_state"] == [
        {"t": "edit_state", "v": protocol.VERSION, "editing": 1}]


def test_a_done_that_overtook_its_ok_is_made_good(tmp_path):
    """Review NICE-1: launch() records the builder before work() queues its
    ok, so a builder that ends in that instant has its done written first,
    and the board, still asking, drops the done and then holds on the ok.
    The next restatement says no builder is open, which frees it."""
    clock = Clock()
    lau, _ = launcher_with(str(tmp_path), lambda u: True)
    returned, go = threading.Event(), threading.Event()

    def opener_then_pause():
        ok = lau.launch()
        returned.set()
        go.wait(5)
        return ok

    def spawn(fn):
        spawn.t = threading.Thread(target=fn)
        spawn.t.start()

    b, sent = wired(tmp_path, lau, clock=clock, run_async=spawn,
                    open_builder=opener_then_pause)
    alive(b, clock)
    b.pump()
    b.on_message({"t": "theme_edit", "v": 2})
    assert returned.wait(2)
    FakeServer.made[0].finish("closed")      # ends in the window
    b.pump()
    go.set()
    spawn.t.join(5)
    b.pump()
    edit = [m for m in sent if m["t"] == "theme_edit"]
    assert edit[-2:] == [{"t": "theme_edit", "v": protocol.VERSION,
                          "done": 1},
                         {"t": "theme_edit", "v": protocol.VERSION, "ok": 1}]
    clock.t += protocol.EDIT_STATE_EVERY_S
    b.on_message({"t": "ping", "v": 2})
    b.pump()
    assert states(sent)[-1] == 0


def test_no_restatement_to_a_board_without_the_handler(tmp_path):
    clock = Clock()
    lau, _ = launcher_with(str(tmp_path), lambda u: True)
    b, sent = wired(tmp_path, lau, clock=clock)
    b.on_message({"t": "hello", "v": 2, "fw": "1.4.0",
                  "caps": ["svc", "theme"]})
    b.on_message({"t": "ping", "v": 2})
    b.pump()
    assert states(sent) == []


def test_the_restatement_never_waits_for_a_slow_browser(tmp_path):
    clock = Clock()
    entered, release = threading.Event(), threading.Event()

    def slow(url):
        entered.set()
        release.wait(5)
        return True

    lau, _ = launcher_with(str(tmp_path), slow)
    b, sent = wired(tmp_path, lau, clock=clock)
    alive(b, clock)
    t = threading.Thread(target=lau.launch)
    t.start()
    try:
        assert entered.wait(2)
        t0 = time.monotonic()
        b.pump()
        assert time.monotonic() - t0 < 0.2
        assert states(sent) == []            # unknown: said next pass
    finally:
        release.set()
        t.join(5)
    b.pump()
    assert states(sent) == [1]


def test_a_failing_state_read_is_not_tried_every_pass(tmp_path, capsys):
    clock = Clock()
    lau, _ = launcher_with(str(tmp_path), lambda u: True)
    calls = []

    def broken():
        calls.append(1)
        raise RuntimeError("boom")

    b, sent = wired(tmp_path, lau, builder_open=broken, clock=clock)
    alive(b, clock)
    n = len(calls)
    b.pump()
    b.pump()
    b.pump()
    assert len(calls) == n + 1
    assert states(sent) == []


def test_the_restatement_fits_and_says_one_thing():
    for editing in (True, False):
        raw, why = protocol.encode_checked(protocol.edit_state(editing))
        assert raw is not None, why
        assert len(raw) < 64
        assert set(json.loads(raw)) == {"t", "v", "editing"}
    assert "edit_state" in protocol.WIRE_TYPES


def test_the_held_slot_says_so_in_sentence_case_and_fits():
    src = _firmware("ui_settings.c")
    m = re.search(r'edit_holding = "([^"]+)"', src)
    assert m
    words = m.group(1)
    assert words == "Editing on your computer"
    assert words[0].isupper() and words[1:] == words[1:].lower()
    # The same length as its two neighbours, which wrap to two lines in the
    # slot's 124 px.
    assert len(words) <= len("Opening on your computer")
