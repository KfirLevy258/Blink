"""A theme save is followed by nothing the board could lose.

Saving a theme makes the board erase and write a flash sector on its main
thread (ui_theme.c THEME_OP_SAVE -> cfg_set_theme -> persist). An ESP32 flash
erase masks every non-IRAM interrupt, the console UART's among them, so for
the length of the erase only the UART's 128-byte FIFO keeps bytes, and at
115200 baud that is about 11 ms of line. cfg_store.c puts the whole erase and
write at "tens of milliseconds". The daemon used to put a usage line on the
wire right behind the save, in the same tick, and the board received it with
its middle missing: page 2 dropped out and the activity pips went wrong until
the next heartbeat. The firmware review simulated it with the real parser and
the daemon's own bytes: with an erase of 30 ms or more, 99 of 99 usage lines
were mis-parsed on an idle main loop (found 2026-10-31).

The fix is on this side only. The save is the last thing a tick writes, the
fast tick ends after one, and the next poll waits out a short bounded hold
that the board's theme_now why=saved ends early.
"""
from pc import protocol
from pc import bridge as bridge_mod
from pc.bridge import Bridge

_NO_FEED = dict(fetch_manifest=lambda: None, fetch_signed_manifest=lambda: None)

SAVE = protocol.theme("save", {"id": 3})
PREVIEW = protocol.theme("preview", {"id": 3})
CANCEL = protocol.theme("cancel")


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


class Sync:
    """What the Bridge sees of pc/theme.ThemeSync: lines owed, once."""

    def __init__(self):
        self.owed = []

    def outgoing(self, deliver=True):
        out, self.owed = (self.owed if deliver else []), []
        return out

    def hello(self):
        pass

    def board_now(self, msg, board_id=None):
        pass


def make(hint=("svc", "theme")):
    sent, clock, sync = [], Clock(), Sync()
    usage = {"n": 0}

    def fetch():
        return protocol.usage(10.0 + usage["n"], None, 20.0, None, [],
                              provider="claude")

    b = Bridge(write_msg=sent.append, fetch_usage=fetch, now=clock,
               wall=lambda: (1752444000, 0), theme_sync=sync,
               board_caps_hint=list(hint), sleep=clock.sleep, **_NO_FEED)
    return b, sent, clock, sync, usage


def kinds(sent):
    return [(m["t"], m.get("op")) for m in sent if m["t"] in ("theme", "usage")]


def test_the_heartbeat_sends_usage_before_a_save():
    b, sent, clock, sync, _ = make()
    sync.owed = [SAVE]
    b.poll_once()
    assert kinds(sent) == [("usage", None), ("theme", "save")]
    assert sent[-1] == SAVE, "nothing at all follows the save in a tick"


def test_the_heartbeat_also_ends_on_the_save_when_usage_is_unreadable():
    b, sent, clock, sync, _ = make()
    b._fetch = lambda: None
    sync.owed = [SAVE]
    b.poll_once()
    assert sent[-1] == SAVE


def test_the_fast_tick_ends_after_a_save_and_usage_goes_next_tick():
    b, sent, clock, sync, usage = make()
    b.poll_once()
    del sent[:]
    usage["n"] = 1                                  # usage moved
    sync.owed = [SAVE]
    b.poll_if_changed()
    assert kinds(sent) == [("theme", "save")]
    clock.t += 2.0                                  # FAST_POLL_INTERVAL_S
    b.poll_if_changed()
    assert kinds(sent) == [("theme", "save"), ("usage", None)]
    assert clock.slept == [], "the fast path never sleeps"


def test_a_preview_does_not_hold_usage_back():
    """Previews and cancels write no flash; a drag must not wait on them."""
    for line in (PREVIEW, CANCEL):
        b, sent, clock, sync, usage = make()
        b.poll_once()
        del sent[:]
        usage["n"] = 1
        sync.owed = [line]
        b.poll_if_changed()
        assert kinds(sent)[0] == ("theme", line["op"])
        assert ("usage", None) in kinds(sent)


def test_a_fast_tick_inside_the_hold_sends_nothing():
    b, sent, clock, sync, usage = make()
    sync.owed = [SAVE]
    b.poll_if_changed()
    del sent[:]
    usage["n"] = 1
    clock.t += 0.1
    b.poll_if_changed()
    assert sent == []


def test_a_heartbeat_inside_the_hold_waits_it_out_and_goes_on():
    b, sent, clock, sync, usage = make()
    sync.owed = [SAVE]
    b.poll_if_changed()
    del sent[:]
    clock.t += 0.1
    b.poll_once()
    assert len(clock.slept) == 1
    assert abs(clock.slept[0] - (bridge_mod.THEME_SAVE_HOLD_S - 0.1)) < 1e-9
    assert kinds(sent) == [("usage", None)]


def test_the_boards_saved_report_ends_the_hold_at_once():
    b, sent, clock, sync, usage = make()
    sync.owed = [SAVE]
    b.poll_if_changed()
    b.on_message({"t": "theme_now", "v": 2, "id": 3, "why": "saved"})
    del sent[:]
    clock.t += 0.01
    b.poll_once()
    assert clock.slept == []
    assert ("usage", None) in kinds(sent)


def test_other_theme_reports_do_not_end_it():
    b, sent, clock, sync, usage = make()
    sync.owed = [SAVE]
    b.poll_if_changed()
    b.on_message({"t": "theme_now", "v": 2, "id": 1, "why": "user"})
    clock.t += 0.01
    b.poll_once()
    assert len(clock.slept) == 1


def test_the_wait_is_bounded_whatever_the_clock_does():
    """It can never stall the serial loop (the board is lost at 30 s of
    silence): at most THEME_SAVE_HOLD_S, once, and not even that if the
    clock stepped back."""
    assert 0 < bridge_mod.THEME_SAVE_HOLD_S <= 1.0
    b, sent, clock, sync, usage = make()
    sync.owed = [SAVE]
    b.poll_if_changed()
    clock.t -= 3600.0
    b.poll_once()
    assert clock.slept and clock.slept[0] <= bridge_mod.THEME_SAVE_HOLD_S
    clock.t += 10
    b.poll_once()
    assert len(clock.slept) == 1, "the hold is spent once it has been waited"


def test_a_board_that_takes_no_theme_is_never_held():
    """No caps, no theme line is written, so there is no save to hold for."""
    b, sent, clock, sync, usage = make(hint=())
    sync.owed = [SAVE]
    b.poll_if_changed()
    usage["n"] = 1
    b.poll_if_changed()
    b.poll_once()
    assert not [m for m in sent if m["t"] == "theme"]
    assert clock.slept == []
    assert ("usage", None) in kinds(sent)


def test_a_new_conversation_forgets_the_hold():
    b, sent, clock, sync, usage = make()
    sync.owed = [SAVE]
    b.poll_if_changed()
    b.greet()
    assert clock.slept == []
