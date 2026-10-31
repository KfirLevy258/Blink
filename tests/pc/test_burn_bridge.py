"""When the burn line goes out (docs/plans/burn-5h.md).

Only to a board whose caps list "burn". Never in the hello burst: on the
first tick after the 2 s quiet window that follows a greeting, the way
themes go. Then whenever q or b changes, at least every 60 s, never within
15 s of the last. When the claude page has a session window again it simply
stops; there is no "off" message.
"""
import ast
import json
import pathlib

from pc import logbook, protocol
from pc.bridge import (BURN_HEARTBEAT_S, BURN_MIN_GAP_S, THEME_QUIET_S,
                       Bridge)

ROOT = pathlib.Path(__file__).resolve().parents[2]
_NO_FEED = dict(fetch_manifest=lambda: None, fetch_signed_manifest=lambda: None)
CAPS = ["svc", "theme", "edit_done", "burn"]
Q = 1_793_430_000
LINE = protocol.burn("claude", Q, [0] * 18 + [4000, 12000])


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Fetch:
    """The bus's fetch: a usage line with the session unknown, and the burn
    line, or None while the claude page has a session window."""

    def __init__(self, line=LINE):
        self.line = line
        self.asked = 0
        self.usage = protocol.usage(-1.0, None, 12.0, None, [],
                                    provider="claude")

    def __call__(self):
        return None if self.usage is None else dict(self.usage)

    def burn(self):
        self.asked += 1
        return None if self.line is None else json.loads(json.dumps(self.line))


def make(fetch=None, hint=None):
    clock = Clock()
    fetch = fetch or Fetch()
    sent = []
    b = Bridge(write_msg=lambda m: sent.append((clock.t, m)),
               fetch_usage=fetch, now=clock, wall=lambda: (1752444000, 0),
               board_caps_hint=hint, **_NO_FEED)
    return b, sent, clock, fetch


def hello(caps=CAPS):
    m = {"t": "hello", "v": 2, "board_id": "ab", "fw": "1.4.0"}
    if caps is not None:
        m["caps"] = caps
    return m


def burns(sent):
    return [(t, m) for t, m in sent if m["t"] == "burn"]


def ticks(b, clock, seconds, every=2.0):
    """The daemon's fast tick for `seconds`, starting one tick from now."""
    end = clock.t + seconds
    while clock.t + every <= end + 1e-9:
        clock.t += every
        b.poll_if_changed()


def test_it_waits_out_the_hello_burst_and_goes_on_the_first_tick_after():
    b, sent, clock, _ = make()
    b.on_message(hello())
    assert burns(sent) == []                    # not in the burst

    clock.t += THEME_QUIET_S - 0.1
    b.poll_if_changed()
    assert burns(sent) == []

    clock.t += 0.1
    b.poll_if_changed()
    assert [m for _, m in burns(sent)] == [LINE]


def test_a_board_that_does_not_list_burn_is_never_sent_one_or_read_for():
    for caps in (["svc", "theme", "edit_done"], None):
        b, sent, clock, fetch = make()
        b.on_message(hello(caps))
        for _ in range(10):
            ticks(b, clock, 58)
            b.poll_once()

        assert burns(sent) == [], caps
        assert fetch.asked == 0, caps           # no transcript read for it


def test_a_change_goes_out_but_never_within_15_s_of_the_last():
    b, sent, clock, fetch = make()
    b.on_message(hello())
    ticks(b, clock, 2)
    first = burns(sent)[-1][0]
    fetch.line = protocol.burn("claude", Q, [0] * 18 + [4000, 13000])

    ticks(b, clock, 30)

    times = [t for t, _ in burns(sent)]
    assert len(times) == 2
    assert BURN_MIN_GAP_S <= times[1] - first < BURN_MIN_GAP_S + 2
    assert burns(sent)[-1][1]["b"][-1] == 13000


def test_an_unchanged_line_is_restated_every_minute():
    b, sent, clock, _ = make()
    b.on_message(hello())
    ticks(b, clock, 2)

    ticks(b, clock, 130)

    times = [t for t, _ in burns(sent)]
    gaps = [b2 - b1 for b1, b2 in zip(times, times[1:])]
    assert len(times) == 3
    assert all(BURN_HEARTBEAT_S <= g < BURN_HEARTBEAT_S + 2 for g in gaps)


def test_a_new_quarter_hour_is_a_change():
    b, sent, clock, fetch = make()
    b.on_message(hello())
    ticks(b, clock, 2)
    fetch.line = protocol.burn("claude", Q + 900, LINE["b"])

    ticks(b, clock, 16)

    assert [m["q"] for _, m in burns(sent)] == [Q, Q + 900]


def test_when_the_session_window_returns_it_stops_with_no_off_message():
    b, sent, clock, fetch = make()
    b.on_message(hello())
    ticks(b, clock, 2)
    kinds_before = {m["t"] for _, m in sent}
    fetch.line = None

    for _ in range(5):
        ticks(b, clock, 58)
        b.poll_once()

    assert len(burns(sent)) == 1
    assert {m["t"] for _, m in sent} == kinds_before


def test_when_it_comes_back_it_goes_out_as_soon_as_the_gap_allows():
    b, sent, clock, fetch = make()
    b.on_message(hello())
    ticks(b, clock, 2)
    fetch.line = None
    ticks(b, clock, 100)
    fetch.line = LINE                           # unchanged, and back

    ticks(b, clock, 2)

    assert len(burns(sent)) == 2


def test_no_usage_reading_means_no_burn_line():
    fetch = Fetch()
    fetch.usage = None
    b, sent, clock, _ = make(fetch)
    b.on_message(hello())
    ticks(b, clock, 60)
    b.poll_once()

    assert burns(sent) == []


def test_the_heartbeat_tick_carries_it_too():
    b, sent, clock, _ = make()
    b.on_message(hello())
    for _ in range(3):
        clock.t += 60
        b.poll_once()

    assert len(burns(sent)) == 3


def test_a_reconnect_waits_its_own_quiet_window_then_sends_again():
    b, sent, clock, _ = make()
    b.on_message(hello())
    ticks(b, clock, 2)
    clock.t += 5
    b.on_message(hello())                       # the board rebooted
    assert len(burns(sent)) == 1

    ticks(b, clock, 2)

    assert len(burns(sent)) == 2                # unchanged, 7 s after: sent


def test_the_remembered_caps_stand_in_until_the_board_speaks():
    b, sent, clock, _ = make(hint=CAPS)
    b.greet()                                   # a restart under a live board
    ticks(b, clock, 2)
    assert len(burns(sent)) == 1

    b.on_message({"t": "pref", "v": 2, "provider": "claude",
                  "caps": ["svc", "theme", "edit_done"]})
    ticks(b, clock, 130)

    assert len(burns(sent)) == 1


def test_a_board_that_lists_burn_again_is_sent_the_line_at_once():
    b, sent, clock, _ = make()
    b.on_message(hello())
    ticks(b, clock, 2)
    b.on_message({"t": "pref", "v": 2, "provider": "claude",
                  "caps": ["svc"]})
    ticks(b, clock, 20)
    b.on_message({"t": "pref", "v": 2, "provider": "claude", "caps": CAPS})

    ticks(b, clock, 2)

    assert len(burns(sent)) == 2                # unchanged, yet sent


def test_a_burn_source_that_raises_is_not_worth_the_serial_loop(capsys):
    fetch = Fetch()

    def boom():
        raise RuntimeError("MARKER /home/someone/.claude/projects/x.jsonl")
    fetch.burn = boom
    b, sent, clock, _ = make(fetch)
    b.on_message(hello())

    ticks(b, clock, 60)

    err = capsys.readouterr().err
    assert "MARKER" not in err and ".claude" not in err
    assert err.count("burn not sent: RuntimeError") == 1
    assert any(m["t"] == "usage" for _, m in sent)


# ---------------------------------------------------------------- the log


def test_the_daemon_logs_burn_by_type_about_once_a_quarter_hour():
    tree = ast.parse((ROOT / "claude_usage_bridge.py").read_text(
        encoding="utf-8"))
    src = ast.unparse(tree)
    assert "'burn': burn_frames" in src
    assert "logbook.Repeats(('p', 'q'), 'burn lines')" in src
    assert "burn_frames.due()" in src

    # Two hours of a desk producing tokens on every tick, through the same
    # Repeats the daemon builds: one line per quarter-hour, not one per send.
    r = logbook.Repeats(("p", "q"), "burn lines")
    logged = []
    b, sent, clock, fetch = make()
    b.on_message(hello())
    for k in range(3600):
        clock.t += 2
        q = Q + 900 * int(k * 2 // 900)
        fetch.line = protocol.burn("claude", q, [0] * 19 + [1000 * k])
        b.poll_if_changed()
    for _, m in burns(sent):
        if r.worth_logging(m):
            logged.append(protocol.log_name(m))

    assert len(burns(sent)) > 400
    assert logged == ["burn"] * 8
