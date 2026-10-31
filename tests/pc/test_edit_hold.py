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


def wired(tmp_path, launcher, builder_open="launcher"):
    home = str(tmp_path / ".blink")
    sent = []

    def write(m):
        raw, why = protocol.encode_checked(m)
        assert raw is not None, why
        sent.append(json.loads(raw))
        return True

    if builder_open == "launcher":
        builder_open = launcher.editing
    b = Bridge(write_msg=write, fetch_usage=lambda: None, now=Clock(),
               wall=lambda: (1752444000, 0), theme_sync=theme.ThemeSync(home),
               open_builder=launcher.launch, builder_ended=launcher.ended,
               builder_open=builder_open, run_async=inline,
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
    # The board finds a cap as a whole member of this list; it must stay a
    # list of plain strings, "theme" still first for the boards before it.
    assert w["caps"][0] == "theme"
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


def test_the_boards_bound_outlasts_the_builders_idle_limit():
    """The board's hold ends by itself after EDIT_HOLD_MS with neither a
    done nor a preview, for a done lost on the wire. A builder no one edits
    ends by itself after IDLE_S and is told to the board at once, so the
    bound must be the longer, or a quiet builder still open would get its
    button back early -- and a second tab."""
    src = _firmware("ui_settings.c")
    m = re.search(r"#define EDIT_HOLD_MS \((\d+) \* (\d+) \* (\d+)\)", src)
    assert m, "EDIT_HOLD_MS not found as (a * b * c) in ui_settings.c"
    hold_ms = int(m.group(1)) * int(m.group(2)) * int(m.group(3))
    assert hold_ms > theme_server.IDLE_S * 1000
    # And no longer than the board's own limit on a preview it never hears
    # the end of: past that the builder's colours are gone from the panel.
    p = re.search(r"#define PREVIEW_MS \((\d+) \* (\d+) \* (\d+)\)",
                  _firmware("ui_theme.c"))
    assert p
    assert hold_ms <= int(p.group(1)) * int(p.group(2)) * int(p.group(3))


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
