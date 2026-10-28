"""The daemon's half of themes: the gate, theme_now, theme_edit, the files."""
import threading

from pc import protocol
from pc import theme
from pc.bridge import Bridge, THEME_QUIET_S

_NO_FEED = dict(fetch_manifest=lambda: None, fetch_signed_manifest=lambda: None)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Opener:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if isinstance(self.ok, Exception):
            raise self.ok
        return self.ok


def inline(fn):
    fn()


def make(tmp_path, opener=None, hint=None, saved=None, run_async=inline):
    home = str(tmp_path / ".blink")
    if saved is not None:
        theme.save(home, saved)          # there before the daemon started
    sent = []
    clock = Clock()
    b = Bridge(write_msg=sent.append, fetch_usage=lambda: None, now=clock,
               wall=lambda: (1752444000, 0), theme_sync=theme.ThemeSync(home),
               open_builder=opener, board_caps_hint=hint, run_async=run_async,
               **_NO_FEED)
    return b, sent, home, clock


def themes(sent):
    return [m for m in sent if m["t"] == "theme"]


def hello(b, fw="1.3.4", caps=("svc", "theme")):
    # The board's caps open the gate (theme contract, Addendum 3), not fw.
    m = {"t": "hello", "v": 2, "fw": fw}
    if caps is not None:
        m["caps"] = list(caps)
    b.on_message(m)


def now(b, tid, why):
    b.on_message({"t": "theme_now", "v": 2, "id": tid, "why": why})


BOARD_A = "a1b2c3d4e5f6"
BOARD_B = "0f1e2d3c4b5a"


def bridge_on(home):
    """A daemon start on `home`: its own ThemeSync, as after a restart."""
    sent = []
    clock = Clock()
    b = Bridge(write_msg=sent.append, fetch_usage=lambda: None, now=clock,
               wall=lambda: (1752444000, 0), theme_sync=theme.ThemeSync(home),
               board_caps_hint=["svc", "theme"], **_NO_FEED)
    return b, sent, clock


def board_hello(b, board_id):
    b.on_message({"t": "hello", "v": 2, "fw": "1.4.0", "board_id": board_id,
                  "caps": ["svc", "theme"]})


def board_pref(b, board_id=None):
    """The pref a board answers a welcome with; 1.4.0 names itself on it
    (contract Addendum 5), older firmware does not."""
    m = {"t": "pref", "v": 2, "provider": "claude", "caps": ["svc", "theme"]}
    if board_id is not None:
        m["board_id"] = board_id
    b.on_message(m)


def tick(b, clock):
    clock.t += THEME_QUIET_S
    b.poll_if_changed()


def neon_seen_on_a(home):
    """theme.json Neon, confirmed on board A by an earlier daemon run, and
    board.json naming A -- as a real run leaves them."""
    import claude_usage_bridge
    theme.save(home, {"id": 1})
    b, sent, clock = bridge_on(home)
    board_hello(b, BOARD_A)
    now(b, 1, "saved")
    claude_usage_bridge.remember_board(
        home, "/dev/cu.test", BOARD_A, "1.4.0", ["svc", "theme"])
    assert theme.load_seen(home)["board_id"] == BOARD_A


def test_a_restart_under_a_board_that_names_itself_on_pref(tmp_path):
    # Review S2 through the Bridge: the daemon restarts under a running
    # board, so no hello; the pref answering the welcome names the board.
    home = str(tmp_path / ".blink")
    neon_seen_on_a(home)
    b, sent, clock = bridge_on(home)
    b.greet()
    board_pref(b, BOARD_A)
    now(b, 4, "boot")                    # Ocean, picked while we were away
    tick(b, clock)
    assert themes(sent) == []
    assert theme.load_saved(home) == {"id": 4}


def test_the_reviewers_board_swap_keeps_theme_json(tmp_path):
    # Review of fp/fix-pc, S1, reproduced: the laptop moves to board B,
    # which holds Ocean. B's hello went to nobody and board.json still names
    # A; B's pref names B. theme.json wins: B is not the board seen.
    home = str(tmp_path / ".blink")
    neon_seen_on_a(home)
    b, sent, clock = bridge_on(home)
    b.greet()
    board_pref(b, BOARD_B)
    now(b, 4, "boot")
    tick(b, clock)
    assert themes(sent) == [theme.message("save", {"id": 1})]
    assert theme.load_saved(home) == {"id": 1}
    assert theme.load_seen(home)["board_id"] == BOARD_B


def test_a_swap_to_old_firmware_with_a_stale_board_json_keeps_theme_json(
        tmp_path):
    # The same swap with a board whose pref does not name it: the only id
    # there is, board.json's, is A's and wrong. It is never used, so
    # theme.json wins and nothing is recorded until the wire names a board.
    home = str(tmp_path / ".blink")
    neon_seen_on_a(home)
    b, sent, clock = bridge_on(home)
    b.greet()
    board_pref(b)
    now(b, 4, "boot")
    tick(b, clock)
    assert themes(sent) == [theme.message("save", {"id": 1})]
    assert theme.load_saved(home) == {"id": 1}
    rec = theme.load_seen(home)
    assert (rec["board_id"], rec["fp"]) == (BOARD_A, {"id": 1})


def test_old_firmware_adopts_only_after_a_hello(tmp_path):
    home = str(tmp_path / ".blink")
    neon_seen_on_a(home)
    b, sent, clock = bridge_on(home)
    board_hello(b, BOARD_A)
    board_pref(b)                        # no id: the hello's stands
    now(b, 4, "boot")
    tick(b, clock)
    assert themes(sent) == []
    assert theme.load_saved(home) == {"id": 4}


def test_a_hello_without_an_id_compares_nothing(tmp_path):
    home = str(tmp_path / ".blink")
    neon_seen_on_a(home)
    for unnamed in ("", "unknown"):      # "unknown": hwinfo had no id
        b, sent, clock = bridge_on(home)
        board_hello(b, unnamed)
        now(b, 4, "boot")
        tick(b, clock)
        assert themes(sent) == [theme.message("save", {"id": 1})]
        assert theme.load_saved(home) == {"id": 1}


def test_the_daemon_hands_the_bridge_no_board_json_id():
    # The connect loop once passed board.json's id in as a hint. Bridge no
    # longer takes one, so it cannot come back by accident.
    import inspect
    import claude_usage_bridge
    assert "board_id_hint" not in inspect.signature(Bridge).parameters
    assert "board_id_hint" not in inspect.getsource(claude_usage_bridge)


def test_welcome_carries_the_capability(tmp_path):
    b, sent, home, _ = make(tmp_path)
    hello(b)
    w = [m for m in sent if m["t"] == "welcome"][0]
    assert w["caps"] == ["theme"]


def test_no_theme_line_rides_the_hello_burst(tmp_path):
    b, sent, home, clock = make(tmp_path, saved={"id": 5})
    theme.write_preview(home, {"id": 2})
    hello(b)
    now(b, 0, "boot")
    b.poll_if_changed()                  # a fast tick due in the same pass
    assert themes(sent) == []
    clock.t += THEME_QUIET_S
    b.poll_if_changed()
    assert themes(sent) == [theme.message("preview", {"id": 2}),
                            theme.message("save", {"id": 5})]
    b.poll_if_changed()
    assert len(themes(sent)) == 2


def test_the_quiet_window_is_one_fast_tick():
    import claude_usage_bridge
    assert THEME_QUIET_S == claude_usage_bridge.FAST_POLL_INTERVAL_S


def test_a_connect_without_a_reset_waits_too(tmp_path):
    # greet() straight from the daemon (already_running) is a burst as well.
    b, sent, home, clock = make(tmp_path, saved={"id": 5}, hint=["svc", "theme"])
    b.greet()
    assert themes(sent) == []
    now(b, 0, "boot")
    clock.t += THEME_QUIET_S
    b.poll_if_changed()
    assert themes(sent) == [theme.message("save", {"id": 5})]
    assert theme.load_saved(home) == {"id": 5}


def test_a_shipped_1_3_4_board_is_sent_no_theme_line(tmp_path):
    b, sent, home, clock = make(tmp_path, saved={"id": 5})
    hello(b, fw="1.3.4", caps=None)
    theme.write_preview(home, {"id": 2})
    clock.t += 10
    b.poll_if_changed()
    b.poll_once()
    assert themes(sent) == []


def test_a_board_not_yet_heard_is_sent_no_theme_line(tmp_path):
    b, sent, home, _ = make(tmp_path)
    theme.save(home, {"id": 5})
    b.poll_if_changed()
    assert themes(sent) == []


def test_the_board_json_caps_open_the_gate_like_svc(tmp_path):
    b, sent, home, _ = make(tmp_path, hint=["svc", "theme"])
    theme.save(home, {"id": 5})
    b.poll_if_changed()
    assert themes(sent) == [theme.message("save", {"id": 5})]


def test_the_fast_tick_sends_a_preview_then_a_save(tmp_path):
    b, sent, home, clock = make(tmp_path)
    hello(b)
    clock.t += THEME_QUIET_S
    theme.write_preview(home, {"id": 8})
    b.poll_if_changed()
    assert themes(sent) == [theme.message("preview", {"id": 8})]
    theme.save(home, {"id": 8})
    b.poll_if_changed()
    b.poll_if_changed()
    assert themes(sent)[1:] == [theme.message("save", {"id": 8})]


def test_the_heartbeat_sends_theme_lines_too(tmp_path):
    b, sent, home, clock = make(tmp_path)
    hello(b)
    clock.t += THEME_QUIET_S
    theme.write_cancel(home)
    b.poll_once()
    assert themes(sent) == [theme.message("cancel")]


def test_a_user_choice_on_the_board_is_kept_and_not_answered(tmp_path):
    b, sent, home, clock = make(tmp_path, saved={"id": 5})
    hello(b)
    now(b, 5, "boot")
    clock.t += 30
    now(b, 1, "user")
    b.poll_if_changed()
    assert themes(sent) == []
    assert theme.load_saved(home) == {"id": 1}


def test_edit_on_computer_opens_the_builder_and_says_so(tmp_path):
    opener = Opener(True)
    b, sent, home, _ = make(tmp_path, opener=opener)
    hello(b)
    b.on_message({"t": "theme_edit", "v": 2})
    assert opener.calls == 1
    assert sent[-1] == {"t": "theme_edit", "v": protocol.VERSION, "ok": 1}


def test_edit_on_computer_with_no_display_says_no(tmp_path):
    b, sent, home, _ = make(tmp_path, opener=Opener(False))
    hello(b)
    b.on_message({"t": "theme_edit", "v": 2})
    assert sent[-1] == {"t": "theme_edit", "v": protocol.VERSION, "ok": 0}


def test_edit_on_computer_with_no_builder_wired_says_no(tmp_path):
    b, sent, home, _ = make(tmp_path, opener=None)
    hello(b)
    b.on_message({"t": "theme_edit", "v": 2})
    assert sent[-1]["ok"] == 0


def test_an_opener_that_raises_says_no_and_keeps_the_daemon_up(tmp_path):
    b, sent, home, _ = make(tmp_path, opener=Opener(OSError("no")))
    hello(b)
    b.on_message({"t": "theme_edit", "v": 2})
    assert sent[-1]["ok"] == 0


def test_the_browser_opens_off_the_serial_thread(tmp_path):
    # The serial loop must not wait on a browser starting: the reply goes
    # out from pump(), on the serial thread, once the open has returned.
    release = threading.Event()
    threads = []

    def slow():
        threads.append(threading.current_thread())
        release.wait(5)
        return True

    def spawn(fn):
        threading.Thread(target=fn, daemon=True).start()

    b, sent, home, _ = make(tmp_path, opener=slow, run_async=spawn)
    hello(b)
    b.on_message({"t": "theme_edit", "v": 2})
    assert [m for m in sent if m["t"] == "theme_edit"] == []
    release.set()
    for _ in range(100):
        b.pump()
        if any(m["t"] == "theme_edit" for m in sent):
            break
        threading.Event().wait(0.02)
    assert sent[-1] == {"t": "theme_edit", "v": protocol.VERSION, "ok": 1}
    assert threads and threads[0] is not threading.current_thread()


def test_every_tap_is_answered(tmp_path):
    # The rate limit lives on the Launcher (pc/theme_server.py), so it
    # survives a reconnect; the Bridge answers each tap, because the board
    # is counting down to its own fallback each time.
    opener = Opener(True)
    b, sent, home, _ = make(tmp_path, opener=opener)
    hello(b)
    for _ in range(3):
        b.on_message({"t": "theme_edit", "v": 2})
    assert [m["ok"] for m in sent if m["t"] == "theme_edit"] == [1, 1, 1]


def test_a_bridge_without_themes_wired_ignores_them(tmp_path):
    sent = []
    b = Bridge(write_msg=sent.append, fetch_usage=lambda: None,
               wall=lambda: (1, 0), **_NO_FEED)
    b.on_message({"t": "hello", "v": 2, "fw": "1.4.0"})
    b.on_message({"t": "theme_now", "v": 2, "id": 3, "why": "user"})
    b.poll_if_changed()
    b.pump()
    assert themes(sent) == []


def test_an_answer_outlives_the_bridge_that_asked(tmp_path):
    # The daemon builds a new Bridge on every reconnect. An open that
    # returns after one is answered on the next, through the queue the
    # daemon keeps for its whole life.
    import queue
    answers = queue.SimpleQueue()
    pending = []
    home = str(tmp_path / ".blink")
    sent1, sent2 = [], []
    b1 = Bridge(write_msg=sent1.append, fetch_usage=lambda: None,
                wall=lambda: (1, 0), theme_sync=theme.ThemeSync(home),
                open_builder=lambda: True, run_async=pending.append,
                edit_answers=answers, **_NO_FEED)
    b1.on_message({"t": "pref", "v": 2, "provider": "claude",
                   "caps": ["svc", "theme"]})
    b1.on_message({"t": "theme_edit", "v": 2})
    b2 = Bridge(write_msg=sent2.append, fetch_usage=lambda: None,
                wall=lambda: (1, 0), theme_sync=theme.ThemeSync(home),
                open_builder=lambda: True, edit_answers=answers, **_NO_FEED)
    pending[0]()                          # the open returns, after reconnect
    b2.pump()
    assert sent2 == [{"t": "theme_edit", "v": protocol.VERSION, "ok": 1}]
