"""The Claude Desktop chat light: pc/desktop_chat_activity.

Every folder is synthetic (tests/support/desktop_chat_fixtures) and every
clock is injected: poll(now) is given the time, so a 50 s hold is checked
without waiting 50 s.
"""
import builtins
import importlib
import io
import os
import random

import pytest

from pc import desktop_chat_activity as dca
from pc import user_config
from tests.support import desktop_chat_fixtures as fx

T0 = 1_800_000_000.0


def _watch(layout, platform, **kw):
    return dca.DesktopChatActivity(
        idb_dir=layout.idb_dir, blob_dir=layout.blob_dir,
        ls_dir=layout.ls_dir, platform=platform, **kw)


def _ev(name, at):
    return {"eventName": name, "eventTimestamp": at}


def _learn_churn(layout, w, sub="1", start=T0):
    """Three idle churn writes, 100 s apart, in `sub`, each confirmed by a
    poll IDLE_CONFIRM_S after it: learned afterwards. Returns the time of
    the last poll."""
    for i, t in enumerate((start + 10, start + 110, start + 210)):
        fx.grow_log(layout.idb_dir, 40)
        fx.add_blob(layout.blob_dir, sub, f"learn-{sub}-{i}")
        assert w.poll(t) is False
        assert w.poll(t + dca.IDLE_CONFIRM_S) is False
    assert sub in w._churn
    return start + 210 + dca.IDLE_CONFIRM_S


class _Sim:
    """A Desktop timeline on an injected clock, polled every `step` seconds.

    Things happen at scheduled times (a log write, a blob, the queue file
    gaining events), always before the poll of the first tick at or after
    them, so a write and the poll that sees it are in the same tick. The
    queue only holds what has "landed", which is how its lateness on macOS
    (20-60 s) is put in: an event has a stamp (when it happened) and a landing
    time (when it reached disk). `scale` multiplies every write size, to show
    that nothing depends on them."""

    def __init__(self, layout, watcher, start, scale=1, step=2.0):
        self.lay = layout
        self.w = watcher
        self.t = start
        self.step = step
        self.scale = scale
        self.on_disk = []           # the events the queue file holds now
        self.todo = []              # (time, serial, fn)
        self.results = []           # (tick time, answer)
        self._serial = 0

    def at(self, when, fn):
        self._serial += 1
        self.todo.append((when, self._serial, fn))

    def log(self, when, n=40):
        self.at(when, lambda: fx.grow_log(self.lay.idb_dir, n * self.scale))

    def blob(self, when, sub, name):
        self.at(when, lambda: fx.add_blob(
            self.lay.blob_dir, sub, name, n_bytes=10 * self.scale))

    def land(self, when, name, stamp):
        """The queue file gains one event, stamped `stamp`, at `when`."""
        def go():
            self.on_disk.append(_ev(name, stamp))
            fx.write_queue(self.lay.ls_dir, self.on_disk)
        self.at(when, go)

    def reply(self, r, length, lag, flag, dblob, sub, tag, blob_every=None):
        """One reply starting at r: log growth every 4 s while it streams,
        its first blob `dblob` s from r (before or after the log), more blobs
        every `blob_every` s, the queue's "sent" (stamped r - 1) landing
        `lag` s after r and its finish (stamped at the end) `flag` s after
        the end."""
        for k in range(0, int(length) + 1, 4):
            self.log(r + k)
        self.blob(r + dblob, sub, f"{tag}-0")
        if blob_every:
            for k in range(1, int(length // blob_every) + 1):
                self.blob(r + k * blob_every, sub, f"{tag}-{k}")
        self.land(r + lag, dca.SENT, r - 1)
        self.land(r + length + flag, "claudeai.message.perceived_ttlt",
                  r + length)
        return r + length

    def run(self, until):
        while self.t <= until:
            due = sorted(x for x in self.todo if x[0] <= self.t)
            self.todo = [x for x in self.todo if x[0] > self.t]
            for _when, _n, fn in due:
                fn()
            self.results.append((self.t, self.w.poll(self.t)))
            self.t += self.step

    def answers(self):
        return [a for _t, a in self.results]


# Replies of every shape: (gap after the last one ended, length, how late
# "sent" lands, how late the finish lands, first blob relative to the start,
# a further blob every N s). Gaps of 5 and 91 s are back to back and just
# past the confirmation wait; the blob is before the log (-4), with it (0)
# and after it (2, 6).
_REPLIES = [
    (10, 20, 20, 0, 0, None),
    (120, 60, 45, 30, -2, None),
    (100, 150, 60, 60, 2, 40),
    (95, 10, 20, 20, 6, None),
    (91, 90, 35, 10, 0, 30),
    (150, 40, 55, 45, -4, None),
    (100, 120, 25, 59, 2, 30),
    (5, 30, 60, 0, 0, None),
    (110, 30, 40, 20, 6, None),
    (200, 80, 30, 15, 0, None),
]


def _lit_with_pending(layout, w, sent_at):
    """A Windows watcher lit by a write at sent_at + 1, with the queue
    holding an unfinished "sent" stamped sent_at."""
    w.poll(sent_at)
    fx.write_queue(layout.ls_dir, [_ev(dca.SENT, sent_at)])
    fx.grow_log(layout.idb_dir, 100)
    assert w.poll(sent_at + 1) is True


# -- Windows ---------------------------------------------------------------

def test_windows_idle_stays_off(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "win32")
    for i in range(20):
        assert w.poll(T0 + 2 * i) is False
    assert w.availability() == dca.AVAILABLE


def test_the_first_poll_is_only_a_baseline(tmp_path):
    lay = fx.make_layout(tmp_path)
    fx.grow_log(lay.idb_dir, 100)
    assert _watch(lay, "win32").poll(T0) is False


def test_windows_growth_lights_and_holds_20_s(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "win32")
    w.poll(T0)
    fx.grow_log(lay.idb_dir, 100)
    assert w.poll(T0 + 2) is True
    assert w.poll(T0 + 12) is True
    assert w.poll(T0 + 22) is True
    assert w.poll(T0 + 22.5) is False


def test_a_same_size_rewrite_counts_by_its_mtime(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "win32")
    w.poll(T0)
    log = os.path.join(lay.idb_dir, "000003.log")
    st = os.stat(log)
    os.utime(log, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert w.poll(T0 + 2) is True


def test_log_rotation_counts_as_activity(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "win32")
    w.poll(T0)
    fx.rotate_log(lay.idb_dir)
    assert w.poll(T0 + 2) is True


def test_macos_hold_is_50_s(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    w.poll(T0)
    end = _learn_churn(lay, w)
    fx.grow_log(lay.idb_dir, 100)              # no blob: a reply
    assert w.poll(end + 100) is True
    assert w.poll(end + 149) is True
    assert w.poll(end + 150) is True
    assert w.poll(end + 151) is False


# -- the queue -------------------------------------------------------------

def test_a_silent_reply_is_bridged_by_a_pending_send(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "win32")
    w.poll(T0)
    fx.write_queue(lay.ls_dir, [_ev(dca.SENT, (T0 + 1) * 1000)])
    fx.grow_log(lay.idb_dir, 100)
    assert w.poll(T0 + 2) is True
    # Windows went 89 s without a write inside one reply.
    for t in range(4, 92, 2):
        assert w.poll(T0 + t) is True
    fx.write_queue(lay.ls_dir, [
        _ev(dca.SENT, (T0 + 1) * 1000),
        _ev("claudeai.message.perceived_ttlt", (T0 + 92) * 1000)])
    fx.grow_log(lay.idb_dir, 100)
    assert w.poll(T0 + 92) is True
    assert w.poll(T0 + 112) is True
    assert w.poll(T0 + 113) is False


def test_a_queue_finish_does_not_cut_an_s1_hold(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "win32")
    w.poll(T0)
    fx.grow_log(lay.idb_dir, 100)
    assert w.poll(T0 + 10) is True
    fx.write_queue(lay.ls_dir, [
        _ev(dca.SENT, T0), _ev("claudeai.message.stopped", T0 + 11)])
    assert w.poll(T0 + 15) is True
    assert w.poll(T0 + 30) is True
    assert w.poll(T0 + 31) is False


def test_a_held_light_with_no_finish_ends_at_the_cap(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "win32")
    _lit_with_pending(lay, w, T0)
    for t in range(60, int(dca.PENDING_CAP_S), 60):
        assert w.poll(T0 + t) is True
    assert w.poll(T0 + dca.PENDING_CAP_S) is True
    assert w.poll(T0 + dca.PENDING_CAP_S + 1) is False


def test_pending_holds_a_lit_light_and_a_finish_lets_it_go(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "win32")
    _lit_with_pending(lay, w, T0)
    assert w.poll(T0 + 50) is True              # past the 20 s hold
    assert w.poll(T0 + 99) is True
    fx.write_queue(lay.ls_dir, [
        _ev(dca.SENT, T0), _ev("claudeai.message.perceived_ttlt", T0 + 100)])
    assert w.poll(T0 + 101) is False


def test_iso_timestamps_and_a_wrapped_list_are_read(tmp_path):
    import datetime
    sent = datetime.datetime(
        2027, 1, 15, 8, tzinfo=datetime.timezone.utc).timestamp()
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "win32")
    w.poll(sent)
    fx.write_queue(lay.ls_dir, {"version": 1, "events": [
        _ev(dca.SENT, "2027-01-15T08:00:00Z")]})
    fx.grow_log(lay.idb_dir, 10)
    assert w.poll(sent + 5) is True
    # Past the 20 s hold: only the ISO "sent", read, keeps it on.
    assert w.poll(sent + 60) is True


def test_a_finish_after_the_send_clears_pending(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "win32")
    w.poll(T0)
    fx.write_queue(lay.ls_dir, [
        _ev(dca.SENT, T0), _ev("claudeai.message.streaming_abandoned",
                               T0 + 3)])
    fx.grow_log(lay.idb_dir, 10)
    assert w.poll(T0 + 5) is True
    assert w.poll(T0 + 25) is True
    assert w.poll(T0 + 26) is False


def test_other_event_names_are_ignored(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "win32")
    fx.write_queue(lay.ls_dir, [_ev("claudeai.something.else", T0)])
    assert w.poll(T0 + 5) is False


@pytest.mark.parametrize("events", [
    "{not json",
    "[1, 2, 3]",
    '"just a string"',
    [{"eventName": dca.SENT, "eventTimestamp": True}],
    [{"eventName": dca.SENT, "eventTimestamp": 99_999_999_999_999}],
    [{"eventName": dca.SENT, "eventTimestamp": 1_000}],
    [{"eventName": dca.SENT, "eventTimestamp": "not a date"}],
    [{"eventName": dca.SENT, "eventTimestamp": None}],
    [{"eventName": dca.SENT, "eventTimestamp": [1]}],
    [{"eventName": 7, "eventTimestamp": T0}],
    [{"eventName": dca.SENT}],
])
def test_a_malformed_queue_is_false_without_raising(tmp_path, events):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "win32")
    fx.write_queue(lay.ls_dir, events)
    assert w.poll(T0 + 5) is False
    assert w.availability() == dca.AVAILABLE


def test_a_missing_local_storage_folder_is_no_queue(tmp_path):
    lay = fx.make_layout(tmp_path)
    os.rmdir(lay.ls_dir)
    w = _watch(lay, "win32")
    assert w.poll(T0) is False
    assert w.availability() == dca.AVAILABLE


# -- macOS churn learning --------------------------------------------------

def test_macos_before_learning_log_growth_alone_does_not_light(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    w.poll(T0)
    fx.grow_log(lay.idb_dir, 100)
    assert w.poll(T0 + 2) is False
    fx.rotate_log(lay.idb_dir)
    assert w.poll(T0 + 4) is False


@pytest.mark.parametrize("case", ["win32", "darwin", "darwin-learned"])
def test_the_queue_alone_never_lights(tmp_path, case):
    lay = fx.make_layout(tmp_path)
    plat = "win32" if case == "win32" else "darwin"
    w = _watch(lay, plat)
    start = T0
    if case == "darwin-learned":
        w.poll(T0)
        start = _learn_churn(lay, w)
    fx.write_queue(lay.ls_dir, [_ev(dca.SENT, start + 1)])
    if case != "darwin-learned":
        assert w.poll(start + 2) is False        # the very first poll
    for t in range(4, 200, 2):
        assert w.poll(start + t) is False


def test_the_queue_alone_never_lights_on_the_first_poll(tmp_path):
    # A daemon starting with a "sent" in the queue whose finish was lost.
    for plat in ("win32", "darwin"):
        lay = fx.make_layout(tmp_path / plat)
        fx.write_queue(lay.ls_dir, [_ev(dca.SENT, T0)])
        assert _watch(lay, plat).poll(T0 + 1) is False


def test_the_queue_cannot_resume_a_light_after_an_unavailable_gap(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "win32")
    _lit_with_pending(lay, w, T0)
    assert w.poll(T0 + 40) is True              # held by the pending send
    moved = lay.idb_dir + ".away"
    os.rename(lay.idb_dir, moved)
    assert w.poll(T0 + 42) is False
    assert w.availability() == dca.UNAVAILABLE
    os.rename(moved, lay.idb_dir)
    for t in range(44, 120, 2):
        assert w.poll(T0 + t) is False          # still pending, not lit


def test_the_queue_cannot_resume_a_light_after_the_setting_was_off(tmp_path):
    lay = fx.make_layout(tmp_path / "desk")
    home = tmp_path / "blink"
    home.mkdir()
    w = _watch(lay, "win32", blink_home=str(home))
    _lit_with_pending(lay, w, T0)
    user_config.put(str(home), dca.SETTING, False)
    assert w.poll(T0 + 10) is False
    user_config.put(str(home), dca.SETTING, True)
    for t in range(12, 120, 2):
        assert w.poll(T0 + t) is False


def test_a_sent_that_arrives_after_the_hold_lapsed_does_not_relight(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "win32")
    w.poll(T0)
    fx.grow_log(lay.idb_dir, 100)
    assert w.poll(T0 + 2) is True
    assert w.poll(T0 + 23) is False             # the 20 s hold ran out
    # The queue's "sent" for that reply reaches disk late, unfinished.
    fx.write_queue(lay.ls_dir, [_ev(dca.SENT, T0 + 1)])
    for t in range(40, 200, 10):
        assert w.poll(T0 + t) is False


def test_learned_churn_pairs_off_and_unpaired_growth_lights(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    w.poll(T0)
    end = _learn_churn(lay, w)
    fx.grow_log(lay.idb_dir, 40)
    fx.add_blob(lay.blob_dir, "1", "c9")
    assert w.poll(end + 70) is False
    # The blob a poll before the log still pairs (within 3 s).
    fx.add_blob(lay.blob_dir, "1", "c10")
    assert w.poll(end + 140) is False
    fx.grow_log(lay.idb_dir, 40)
    assert w.poll(end + 142) is False
    # Log growth with no churn blob near it: a reply.
    fx.grow_log(lay.idb_dir, 40)
    assert w.poll(end + 200) is True


def test_a_burst_of_blobs_counts_once(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    w.poll(T0)
    for i, t in enumerate((10, 30, 50, 69)):
        fx.grow_log(lay.idb_dir, 40)
        fx.add_blob(lay.blob_dir, "1", f"a{i}")
        assert w.poll(T0 + t) is False
    assert w._candidates == {"1": T0 + 10}      # one, the earliest
    fx.grow_log(lay.idb_dir, 40)
    assert w.poll(T0 + 100) is False
    assert w._occasions == {"1": (1, T0 + 10)}
    assert "1" not in w._churn
    for i, t in enumerate((110, 210)):
        fx.grow_log(lay.idb_dir, 40)
        fx.add_blob(lay.blob_dir, "1", f"b{i}")
        w.poll(T0 + t)
        w.poll(T0 + t + dca.IDLE_CONFIRM_S)
    assert "1" in w._churn
    fx.grow_log(lay.idb_dir, 40)
    assert w.poll(T0 + 400) is True


def test_genuine_idle_churn_is_learned_by_candidate_time(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    w.poll(T0)
    _learn_churn(lay, w)
    # Three occasions, timed when each blob was seen, not when confirmed.
    assert w._occasions["1"] == (3, T0 + 210)
    assert w._candidates == {}


def test_a_candidate_is_confirmed_only_after_90_s(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    w.poll(T0)
    fx.add_blob(lay.blob_dir, "1", "c")
    w.poll(T0 + 10)
    assert w._candidates == {"1": T0 + 10}
    w.poll(T0 + 10 + dca.IDLE_CONFIRM_S - 0.5)
    assert w._occasions == {} and "1" in w._candidates
    w.poll(T0 + 10 + dca.IDLE_CONFIRM_S)
    assert w._occasions == {"1": (1, T0 + 10)} and w._candidates == {}


@pytest.mark.parametrize("events", [
    [_ev(dca.SENT, T0 + 8), _ev("claudeai.message.perceived_ttlt", T0 + 20)],
    [_ev("claudeai.message.stopped", T0 + 15)],
    [_ev(dca.SENT, T0 + 8)],                    # still pending
])
def test_a_late_queue_event_drops_a_candidate(tmp_path, events):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    w.poll(T0)
    fx.add_blob(lay.blob_dir, "1", "c")
    w.poll(T0 + 10)
    assert "1" in w._candidates
    fx.write_queue(lay.ls_dir, events)          # 40 s late, as on macOS
    w.poll(T0 + 50)
    assert w._candidates == {}
    w.poll(T0 + 200)
    assert w._occasions == {}


def test_an_old_queue_event_does_not_drop_a_candidate(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    fx.write_queue(lay.ls_dir, [
        _ev(dca.SENT, T0), _ev("claudeai.message.stopped", T0 + 5)])
    w.poll(T0 + 200)
    fx.add_blob(lay.blob_dir, "1", "c")
    w.poll(T0 + 300)
    w.poll(T0 + 300 + dca.IDLE_CONFIRM_S)
    assert w._occasions == {"1": (1, T0 + 300)}


@pytest.mark.parametrize("later_write", [True, False])
def test_a_candidate_is_dropped_when_the_light_comes_on(tmp_path,
                                                        later_write):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    w.poll(T0)
    end = _learn_churn(lay, w)
    # A blob in a sub-folder that is not churn lights by itself; that
    # light is its own and does not drop it.
    fx.add_blob(lay.blob_dir, "2", "x")
    assert w.poll(end + 100) is True
    assert "2" in w._candidates
    if later_write:
        fx.grow_log(lay.idb_dir, 40)            # unpaired: the light again
        assert w.poll(end + 110) is True
        assert w._candidates == {}
    w.poll(end + 300)
    assert ("2" in w._occasions) is (not later_write)


def test_candidates_do_not_survive_an_unavailable_gap(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    w.poll(T0)
    fx.add_blob(lay.blob_dir, "1", "c")
    w.poll(T0 + 10)
    assert "1" in w._candidates
    moved = lay.blob_dir + ".away"
    os.rename(lay.blob_dir, moved)
    assert w.poll(T0 + 20) is False
    assert w._candidates == {}
    os.rename(moved, lay.blob_dir)
    w.poll(T0 + 30)
    w.poll(T0 + 200)
    assert w._occasions == {}


def test_reply_after_reply_does_not_teach_its_own_subfolder(tmp_path):
    """The reproduced bug: each reply writes a new blob in one sub-folder,
    and its "sent" reaches the queue 20-60 s later. Counting the blob at
    once taught that sub-folder as churn after three replies, and the
    fourth never lit."""
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    w.poll(T0)
    events = []
    lags = (20, 45, 60, 30, 55, 40)
    for i, lag in enumerate(lags):
        r = T0 + 10 + 150 * i                   # replies 150 s apart
        fx.grow_log(lay.idb_dir, 40)
        fx.add_blob(lay.blob_dir, "1", f"r{i}")
        w.poll(r)
        events += [_ev(dca.SENT, r - 1),
                   _ev("claudeai.message.perceived_ttlt", r + 15)]
        for t in range(10, 150, 10):
            if t >= lag and t - 10 < lag:
                fx.write_queue(lay.ls_dir, events)
            w.poll(r + t)
    assert "1" not in w._churn
    assert "1" not in w._occasions
    # Real idle churn in another sub-folder is still learned, well after
    # the last chat, and a reply in "1" still lights.
    end = _learn_churn(lay, w, sub="2", start=T0 + 1200)
    assert "1" not in w._churn
    fx.grow_log(lay.idb_dir, 40)
    fx.add_blob(lay.blob_dir, "1", "reply")
    assert w.poll(end + 100) is True


def test_a_changed_subfolder_set_forgets_learning(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    w.poll(T0)
    end = _learn_churn(lay, w)
    fx.rename_subfolders(lay.blob_dir, {"1": "7", "2": "8"})
    assert w.poll(end + 10) is False
    assert w._churn == set()
    fx.grow_log(lay.idb_dir, 40)
    assert w.poll(end + 20) is False            # nothing learned: no light


def test_a_subfolder_change_while_unreadable_forgets_learning(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    w.poll(T0)
    end = _learn_churn(lay, w)
    moved = lay.blob_dir + ".away"
    os.rename(lay.blob_dir, moved)
    assert w.poll(end + 10) is False
    assert w.availability() == dca.UNAVAILABLE
    assert "1" in w._churn                      # nothing to compare yet
    fx.rename_subfolders(moved, {"1": "7"})
    os.rename(moved, lay.blob_dir)
    w.poll(end + 20)
    assert w._churn == set() and w._occasions == {}


def test_a_subfolder_change_while_off_forgets_learning(tmp_path):
    lay = fx.make_layout(tmp_path / "desk")
    home = tmp_path / "blink"
    home.mkdir()
    w = _watch(lay, "darwin", blink_home=str(home))
    w.poll(T0)
    end = _learn_churn(lay, w)
    user_config.put(str(home), dca.SETTING, False)
    assert w.poll(end + 10) is False
    fx.rename_subfolders(lay.blob_dir, {"2": "9"})
    user_config.put(str(home), dca.SETTING, True)
    w.poll(end + 20)
    assert w._churn == set()


def test_an_unchanged_subfolder_set_across_a_gap_keeps_learning(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    w.poll(T0)
    end = _learn_churn(lay, w)
    moved = lay.blob_dir + ".away"
    os.rename(lay.blob_dir, moved)
    w.poll(end + 10)
    os.rename(moved, lay.blob_dir)
    w.poll(end + 20)
    assert w._churn == {"1"}


def test_a_new_blob_in_a_non_churn_subfolder_lights(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    w.poll(T0)
    end = _learn_churn(lay, w)
    fx.add_blob(lay.blob_dir, "2", "reply")
    assert w.poll(end + 100) is True


def test_learning_waits_while_a_send_is_pending(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    w.poll(T0)
    fx.write_queue(lay.ls_dir, [_ev(dca.SENT, T0)])
    for i, t in enumerate((10, 110, 210)):
        fx.grow_log(lay.idb_dir, 40)
        fx.add_blob(lay.blob_dir, "1", f"c{i}")
        assert w.poll(T0 + t) is False          # the queue alone: no light
        assert w._candidates == {}
        assert w.poll(T0 + t + dca.IDLE_CONFIRM_S) is False
    assert w._churn == set() and w._occasions == {}


def test_no_blob_folder_on_macos_is_unavailable(tmp_path):
    # Without a blob listing the macOS rule cannot tell churn from a reply,
    # so it must not claim the signal. A pending send does not change that.
    lay = fx.make_layout(tmp_path, blob_subs=())
    os.rmdir(lay.blob_dir)
    fx.write_queue(lay.ls_dir, [_ev(dca.SENT, T0)])
    w = _watch(lay, "darwin")
    assert w.poll(T0 + 1) is False
    assert w.availability() == dca.UNAVAILABLE
    assert w._log is None and w._blobs is None


def test_a_blob_folder_that_is_a_file_on_macos_is_unavailable(tmp_path):
    lay = fx.make_layout(tmp_path, blob_subs=())
    os.rmdir(lay.blob_dir)
    with open(lay.blob_dir, "wb") as fh:
        fh.write(b"x")
    w = _watch(lay, "darwin")
    assert w.poll(T0) is False
    assert w.availability() == dca.UNAVAILABLE


def test_a_blob_folder_that_goes_away_forgets_the_baseline(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    assert w.poll(T0) is False
    assert w.availability() == dca.AVAILABLE
    moved = lay.blob_dir + ".away"
    os.rename(lay.blob_dir, moved)
    fx.grow_log(lay.idb_dir, 40)
    assert w.poll(T0 + 2) is False
    assert w.availability() == dca.UNAVAILABLE
    assert w._log is None and w._blobs is None
    os.rename(moved, lay.blob_dir)
    # Back again: the first poll is a fresh baseline, not a reply.
    assert w.poll(T0 + 4) is False
    assert w.availability() == dca.AVAILABLE


@pytest.mark.skipif(not (os.name == "posix" and hasattr(os, "geteuid")
                         and os.geteuid() != 0),
                    reason="needs POSIX and a non-root user")
def test_an_unreadable_blob_folder_on_macos_is_unavailable(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    os.chmod(lay.blob_dir, 0)
    try:
        assert w.poll(T0) is False
        assert w.availability() == dca.UNAVAILABLE
    finally:
        os.chmod(lay.blob_dir, 0o755)


# -- what counts as a log ----------------------------------------------------

@pytest.mark.parametrize("plat", ["win32", "darwin"])
def test_a_folder_named_like_a_log_is_not_a_log(tmp_path, plat):
    lay = fx.make_layout(tmp_path)
    os.remove(os.path.join(lay.idb_dir, "000003.log"))
    os.mkdir(os.path.join(lay.idb_dir, "000003.log"))
    w = _watch(lay, plat)
    assert w.poll(T0) is False
    assert w.availability() == dca.UNAVAILABLE


@pytest.mark.parametrize("plat", ["win32", "darwin"])
def test_a_higher_numbered_folder_does_not_hide_the_real_log(tmp_path, plat):
    lay = fx.make_layout(tmp_path)
    os.mkdir(os.path.join(lay.idb_dir, "000009.log"))
    w = _watch(lay, plat)
    assert w.poll(T0) is False
    assert w.availability() == dca.AVAILABLE
    assert w._log[0] == 3
    if plat == "win32":
        # Written straight to the real log: fx.grow_log picks the highest
        # number, which here is the folder.
        with open(os.path.join(lay.idb_dir, "000003.log"), "ab") as fh:
            fh.write(os.urandom(10))
        assert w.poll(T0 + 2) is True


def _symlink_or_skip(target, link):
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("this platform will not make a symlink here")


@pytest.mark.parametrize("plat", ["win32", "darwin"])
def test_a_symlink_named_like_a_log_is_not_followed(tmp_path, plat):
    # Deliberate: a leveldb never makes a symlink called N.log, so one is a
    # layout we do not recognise, even when it points at a real file.
    lay = fx.make_layout(tmp_path)
    real = os.path.join(lay.idb_dir, "000003.log")
    elsewhere = str(tmp_path / "elsewhere.bin")
    os.rename(real, elsewhere)
    _symlink_or_skip(elsewhere, real)
    w = _watch(lay, plat)
    assert w.poll(T0) is False
    assert w.availability() == dca.UNAVAILABLE


@pytest.mark.parametrize("plat", ["win32", "darwin"])
def test_a_higher_numbered_symlink_does_not_hide_the_real_log(tmp_path, plat):
    lay = fx.make_layout(tmp_path)
    _symlink_or_skip(os.path.join(lay.idb_dir, "000003.log"),
                     os.path.join(lay.idb_dir, "000009.log"))
    w = _watch(lay, plat)
    assert w.poll(T0) is False
    assert w.availability() == dca.AVAILABLE
    assert w._log[0] == 3


def test_a_folder_of_only_non_file_logs_is_unavailable_then_recovers(
        tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "win32")
    real = os.path.join(lay.idb_dir, "000003.log")
    os.rename(real, real + ".away")
    os.mkdir(real)
    assert w.poll(T0) is False
    assert w.availability() == dca.UNAVAILABLE
    os.rmdir(real)
    os.rename(real + ".away", real)
    assert w.poll(T0 + 2) is False              # a fresh baseline
    assert w.availability() == dca.AVAILABLE
    fx.grow_log(lay.idb_dir, 10)
    assert w.poll(T0 + 4) is True


# -- the blob folder, per platform -------------------------------------------

def test_windows_does_not_need_a_blob_folder(tmp_path):
    lay = fx.make_layout(tmp_path, blob_subs=())
    os.rmdir(lay.blob_dir)
    w = _watch(lay, "win32")
    assert w.poll(T0) is False
    assert w.availability() == dca.AVAILABLE
    fx.grow_log(lay.idb_dir, 10)
    assert w.poll(T0 + 2) is True


def test_windows_with_a_blob_folder_that_is_a_file_is_available(tmp_path):
    lay = fx.make_layout(tmp_path, blob_subs=())
    os.rmdir(lay.blob_dir)
    with open(lay.blob_dir, "wb") as fh:
        fh.write(b"x")
    w = _watch(lay, "win32")
    assert w.poll(T0) is False
    assert w.availability() == dca.AVAILABLE


def test_macos_blob_folder_gone_mid_hold_drops_the_light(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    w.poll(T0)
    end = _learn_churn(lay, w)
    fx.grow_log(lay.idb_dir, 10)
    assert w.poll(end + 100) is True
    moved = lay.blob_dir + ".away"
    os.rename(lay.blob_dir, moved)
    assert w.poll(end + 102) is False
    assert w.availability() == dca.UNAVAILABLE
    os.rename(moved, lay.blob_dir)
    assert w.poll(end + 104) is False           # baseline again, no hold
    assert w.availability() == dca.AVAILABLE


# -- no size threshold -------------------------------------------------------

def _scripted(tmp_path, scale):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    out = [w.poll(T0)]
    for i, t in enumerate((10, 110, 210)):
        fx.grow_log(lay.idb_dir, 40 * scale)
        fx.add_blob(lay.blob_dir, "1", f"c{i}", n_bytes=10 * scale)
        out.append(w.poll(T0 + t))
        out.append(w.poll(T0 + t + dca.IDLE_CONFIRM_S))
    steps = [(500, 30, None), (502, 50, None), (560, 0, None),
             (600, 20, "1"), (630, 0, "2"), (700, 0, None)]
    for t, grow, blob in steps:
        if grow:
            fx.grow_log(lay.idb_dir, grow * scale)
        if blob:
            fx.add_blob(lay.blob_dir, blob, f"s{t}", n_bytes=10 * scale)
        out.append(w.poll(T0 + t))
    return out


def test_scaling_every_write_by_100x_gives_the_same_sequence(tmp_path):
    small = _scripted(tmp_path / "small", 1)
    big = _scripted(tmp_path / "big", 100)
    assert small == big
    assert True in small and small[-1] is False


# -- availability and the setting -------------------------------------------

def test_a_missing_folder_is_unavailable(tmp_path):
    w = dca.DesktopChatActivity(
        idb_dir=str(tmp_path / "nope"), blob_dir=str(tmp_path / "nb"),
        ls_dir=str(tmp_path / "nl"), platform="win32")
    assert w.poll(T0) is False
    assert w.availability() == dca.UNAVAILABLE


def test_a_folder_with_no_log_is_unavailable(tmp_path):
    lay = fx.make_layout(tmp_path)
    os.remove(os.path.join(lay.idb_dir, "000003.log"))
    w = _watch(lay, "darwin")
    assert w.poll(T0) is False
    assert w.availability() == dca.UNAVAILABLE


def test_an_unsupported_platform_is_unavailable(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "linux")
    fx.write_queue(lay.ls_dir, [_ev(dca.SENT, T0)])
    assert w.poll(T0 + 1) is False
    assert w.availability() == dca.UNAVAILABLE


def test_an_unreadable_folder_is_false_without_raising(tmp_path,
                                                       monkeypatch):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "win32")

    def refuse(path):
        raise PermissionError(13, "refused")
    monkeypatch.setattr(dca.os, "scandir", refuse)
    assert w.poll(T0) is False
    assert w.availability() == dca.UNAVAILABLE


def test_an_unexpected_error_is_false_without_raising(tmp_path, monkeypatch):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "win32")

    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(dca, "_newest_log", boom)
    assert w.poll(T0) is False


def test_availability_before_any_poll_polls(tmp_path):
    lay = fx.make_layout(tmp_path)
    assert _watch(lay, "win32").availability() == dca.AVAILABLE


def test_setting_off_is_off_and_false(tmp_path):
    lay = fx.make_layout(tmp_path / "desk")
    home = tmp_path / "blink"
    home.mkdir()
    user_config.put(str(home), dca.SETTING, False)
    w = _watch(lay, "win32", blink_home=str(home))
    fx.write_queue(lay.ls_dir, [_ev(dca.SENT, T0)])
    assert w.poll(T0 + 1) is False
    assert w.availability() == dca.OFF
    assert dca.enabled(str(home)) is False
    assert dca.config_state(str(home)) is False


def test_turning_it_back_on_starts_from_a_fresh_baseline(tmp_path):
    lay = fx.make_layout(tmp_path / "desk")
    home = tmp_path / "blink"
    home.mkdir()
    w = _watch(lay, "win32", blink_home=str(home))
    w.poll(T0)
    user_config.put(str(home), dca.SETTING, False)
    w.poll(T0 + 2)
    fx.grow_log(lay.idb_dir, 100)
    user_config.put(str(home), dca.SETTING, True)
    assert w.poll(T0 + 4) is False
    assert w.availability() == dca.AVAILABLE


def test_no_config_means_on(tmp_path):
    home = tmp_path / "blink"
    home.mkdir()
    assert dca.enabled(str(home)) is True
    assert dca.config_state(str(home)) is True
    lay = fx.make_layout(tmp_path / "desk")
    w = _watch(lay, "win32", blink_home=str(home))
    w.poll(T0)
    assert w.availability() == dca.AVAILABLE


def test_an_unreadable_config_is_none_for_status_and_on_for_the_poll(
        tmp_path):
    home = tmp_path / "blink"
    home.mkdir()
    (home / user_config.FILE_NAME).write_text("{half")
    assert dca.config_state(str(home)) is None
    assert dca.enabled(str(home)) is dca.DEFAULT_ON


# -- what it never does ------------------------------------------------------

SENTINEL = b"SENTINEL-7f3a-DO-NOT-PRINT"


def test_nothing_from_the_stores_reaches_stdout_or_stderr(tmp_path, capsys):
    lay = fx.make_layout(tmp_path)
    for d in (lay.idb_dir,):
        with open(os.path.join(d, "000003.log"), "ab") as fh:
            fh.write(SENTINEL)
    fx.add_blob(lay.blob_dir, "1", "SENTINEL-name")
    fx.write_queue(lay.ls_dir, [
        {"eventName": dca.SENT, "eventTimestamp": T0,
         "payload": SENTINEL.decode()}])
    for plat in ("darwin", "win32", "linux"):
        w = _watch(lay, plat)
        for i in range(5):
            fx.grow_log(lay.idb_dir, 10)
            w.poll(T0 + 2 * i)
        w.availability()
    out, err = capsys.readouterr()
    assert out == "" and err == ""


def test_the_indexeddb_and_blob_folders_are_never_opened(tmp_path,
                                                         monkeypatch):
    lay = fx.make_layout(tmp_path)
    fx.write_queue(lay.ls_dir, [_ev(dca.SENT, T0)])
    opened = []
    real_open = builtins.open
    real_io_open = io.open

    def rec(real):
        def opener(file, *a, **k):
            opened.append(os.fspath(file) if not isinstance(file, int)
                          else file)
            return real(file, *a, **k)
        return opener
    monkeypatch.setattr(builtins, "open", rec(real_open))
    monkeypatch.setattr(io, "open", rec(real_io_open))
    for plat in ("darwin", "win32"):
        w = _watch(lay, plat)
        w.poll(T0)
        monkeypatch.undo()
        fx.grow_log(lay.idb_dir, 10)
        fx.add_blob(lay.blob_dir, "2", f"n-{plat}")
        monkeypatch.setattr(builtins, "open", rec(real_open))
        monkeypatch.setattr(io, "open", rec(real_io_open))
        w.poll(T0 + 2)
    monkeypatch.undo()
    assert opened, "the recorder saw nothing; the Local Storage read opens"
    idb = os.path.realpath(lay.idb_dir)
    blob = os.path.realpath(lay.blob_dir)
    for p in opened:
        if isinstance(p, int):
            continue
        real = os.path.realpath(p)
        assert not real.startswith(idb + os.sep) and real != idb
        assert not real.startswith(blob + os.sep) and real != blob


def test_importing_the_module_does_nothing(monkeypatch):
    calls = []

    def rec(name):
        def f(*a, **k):
            calls.append(name)
            raise AssertionError(name)
        return f
    for name in ("scandir", "listdir", "stat"):
        monkeypatch.setattr(os, name, rec(name))
    monkeypatch.setattr(os.path, "expanduser", rec("expanduser"))
    monkeypatch.setattr(builtins, "print", rec("print"))
    importlib.reload(dca)
    monkeypatch.undo()
    assert calls == []


# -- holds at the edge, with no byte-size threshold --------------------------

@pytest.mark.parametrize("plat,hold", [("win32", 20.0), ("darwin", 50.0)])
@pytest.mark.parametrize("n_bytes", [1, 100_000])
def test_the_hold_is_exact_whatever_the_write_size(tmp_path, plat, hold,
                                                   n_bytes):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, plat)
    w.poll(T0)
    t = T0 + 10
    if plat == "darwin":
        t = _learn_churn(lay, w) + 100
    fx.grow_log(lay.idb_dir, n_bytes)
    assert w.poll(t) is True
    assert w.poll(t + hold) is True
    assert w.poll(t + hold + 0.001) is False


def test_windows_does_not_scan_the_blob_folder(tmp_path, monkeypatch):
    lay = fx.make_layout(tmp_path)

    def boom(*a, **k):
        raise AssertionError("blob folder scanned on Windows")
    monkeypatch.setattr(dca, "_blob_names", boom)
    w = _watch(lay, "win32")
    w.poll(T0)
    fx.add_blob(lay.blob_dir, "2", "x")
    assert w.poll(T0 + 2) is False


def test_macos_before_learning_a_new_blob_does_not_light(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    w.poll(T0)
    fx.grow_log(lay.idb_dir, 10)
    fx.add_blob(lay.blob_dir, "2", "x")
    assert w.poll(T0 + 2) is False


def test_a_far_future_stamp_neither_hides_nor_cancels_a_real_send(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "win32")
    far = T0 + 10 * 86400
    w.poll(T0 + 1)
    fx.write_queue(lay.ls_dir, [_ev(dca.SENT, T0), _ev(dca.SENT, far)])
    fx.grow_log(lay.idb_dir, 10)
    assert w.poll(T0 + 3) is True
    assert w.poll(T0 + 30) is True              # past the hold: pending
    fx.write_queue(lay.ls_dir, [
        _ev(dca.SENT, T0), _ev("claudeai.message.stopped", far)])
    assert w.poll(T0 + 31) is True


def test_a_log_deleted_between_listing_and_stat_is_not_unavailable(
        tmp_path, monkeypatch):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "win32")
    w.poll(T0)
    real_stat = os.stat
    fired = []

    def flaky(path, *a, **k):
        if str(path).endswith(".log") and lay.idb_dir in str(path) \
                and not fired:
            fired.append(1)
            raise FileNotFoundError(2, "gone")
        return real_stat(path, *a, **k)
    fx.grow_log(lay.idb_dir, 10)
    monkeypatch.setattr(dca.os, "stat", flaky)
    assert w.poll(T0 + 2) is True
    assert fired
    assert w.availability() == dca.AVAILABLE


def test_the_three_folders_come_from_one_resolve(tmp_path, monkeypatch):
    from pc import desktop_paths
    calls = []
    real = desktop_paths._resolve

    def counting():
        calls.append(1)
        return real()
    monkeypatch.setattr(desktop_paths, "_resolve", counting)
    dca.DesktopChatActivity(platform="darwin").poll(T0)
    assert len(calls) == 1


# -- failure handling ---------------------------------------------------------

# owner True: a method of the class. Looked up at run time, not collection:
# test_importing_the_module_does_nothing reloads the module, which makes a
# new class, and a class captured here would be the stale one.
@pytest.mark.parametrize("plat,target,owner", [
    ("win32", "_newest_log", False), ("darwin", "_newest_log", False),
    ("darwin", "_blob_names", False),
    ("win32", "_queue_events", True), ("darwin", "_queue_events", True),
    ("win32", "_pending", True), ("darwin", "_pending", True),
    ("darwin", "_blob_changes", True),
    ("win32", "_paths", True),
])
@pytest.mark.parametrize("control", [False, True])
def test_an_exception_in_any_step_is_false(tmp_path, monkeypatch, plat,
                                           target, owner, control):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, plat)
    w.poll(T0)
    t = T0 + 2
    if plat == "darwin":
        t = _learn_churn(lay, w) + 100
    fx.write_queue(lay.ls_dir, [_ev(dca.SENT, t - 1)])
    fx.grow_log(lay.idb_dir, 40)                # unpaired: a reply

    def boom(*a, **k):
        raise RuntimeError("boom")
    if not control:
        monkeypatch.setattr(dca.DesktopChatActivity if owner else dca,
                            target, boom)
    # The control proves this poll would answer True, so False with the
    # error proves the whole poll gave up rather than going on.
    assert w.poll(t) is control


@pytest.mark.parametrize("where", [
    ("leveldb", "scan"), ("desktop_local_storage", "decode_value")])
def test_an_exception_in_a_dependency_is_false(tmp_path, monkeypatch, where):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "win32")
    _lit_with_pending(lay, w, T0)
    assert w.poll(T0 + 30) is True              # past the hold: pending

    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(getattr(dca, where[0]), where[1], boom)
    # A queue that fails to read is no queue: no pending, the light drops.
    assert w.poll(T0 + 32) is False


def test_an_unreadable_config_does_not_turn_the_light_off(tmp_path,
                                                          monkeypatch):
    """By design (enabled()): a failed config read is the default, on."""
    lay = fx.make_layout(tmp_path / "desk")
    home = tmp_path / "blink"
    home.mkdir()
    w = _watch(lay, "win32", blink_home=str(home))

    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(dca.user_config, "load", boom)
    w.poll(T0)
    fx.grow_log(lay.idb_dir, 10)
    assert w.poll(T0 + 1) is True


def test_an_exception_in_path_resolution_is_false(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(dca.desktop_paths, "indexeddb_dir", boom)
    assert dca.DesktopChatActivity(platform="win32").poll(T0) is False


def _chmod_ok():
    return os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() != 0


@pytest.mark.skipif(not _chmod_ok(), reason="needs POSIX and a non-root user")
@pytest.mark.parametrize("which", ["idb", "blob", "ls"])
@pytest.mark.parametrize("plat", ["win32", "darwin"])
def test_an_unreadable_directory_is_false(tmp_path, which, plat):
    lay = fx.make_layout(tmp_path)
    fx.write_queue(lay.ls_dir, [_ev(dca.SENT, T0)])
    target = {"idb": lay.idb_dir, "blob": lay.blob_dir,
              "ls": lay.ls_dir}[which]
    w = _watch(lay, plat)
    os.chmod(target, 0)
    # A refused folder never raises and never lights; nor does the pending
    # send, which can only hold a light an IndexedDB write switched on. A
    # refused blob folder is nothing to Windows, which never reads it, and
    # makes macOS unavailable, where it is half of the rule.
    try:
        for i in range(3):
            assert w.poll(T0 + 2 * i) is False
        if which == "idb" or (which == "blob" and plat == "darwin"):
            assert w.availability() == dca.UNAVAILABLE
    finally:
        os.chmod(target, 0o755)


@pytest.mark.skipif(not _chmod_ok(), reason="needs POSIX and a non-root user")
def test_an_unreadable_blob_subfolder_does_not_raise(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    sub = os.path.join(lay.blob_dir, "1")
    os.chmod(sub, 0)
    try:
        assert w.poll(T0) is False
        assert w.poll(T0 + 2) is False
        # Refused is not empty (2026-11-25): the layout was not read.
        assert w.availability() == dca.UNAVAILABLE
    finally:
        os.chmod(sub, 0o755)


# A refused nested folder, put in by monkeypatching os.scandir for that one
# path rather than by chmod, so it runs on Windows and as root too. Until
# 2026-11-25 _blob_names swallowed the error and read the folder as empty:
# `blink status` said "available" for a layout it had not read, and every
# name in it was "new" the poll it came back.

def _fail_scandir(monkeypatch, path, exc):
    """os.scandir raises `exc` for `path` while `state["on"]` is true."""
    real = os.scandir
    target = os.path.normcase(os.path.normpath(path))
    state = {"on": True, "hits": 0}

    def fake(p=".", *a, **k):
        if state["on"] and os.path.normcase(
                os.path.normpath(os.fspath(p))) == target:
            state["hits"] += 1
            raise exc
        return real(p, *a, **k)
    monkeypatch.setattr(dca.os, "scandir", fake)
    return state


@pytest.mark.parametrize("depth", [("1",), ("1", "00")],
                         ids=["subfolder", "folder-inside-one"])
def test_a_refused_nested_blob_folder_is_unavailable(tmp_path, monkeypatch,
                                                     depth):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    assert w.poll(T0) is False
    assert w.availability() == dca.AVAILABLE
    state = _fail_scandir(monkeypatch, os.path.join(lay.blob_dir, *depth),
                          PermissionError(13, "refused"))
    assert w.poll(T0 + 2) is False
    assert state["hits"]
    assert w.availability() == dca.UNAVAILABLE
    assert dca._blob_names(lay.blob_dir) is None


def test_a_refused_is_dir_inside_a_subfolder_is_unavailable(tmp_path,
                                                             monkeypatch):
    lay = fx.make_layout(tmp_path)
    real = os.scandir
    target = os.path.normcase(os.path.join(lay.blob_dir, "1"))

    class _Entry:
        def __init__(self, e):
            self._e = e
            self.name = e.name
            self.path = e.path

        def is_dir(self, follow_symlinks=True):
            raise PermissionError(13, "refused")

    class _It:
        def __init__(self, it):
            self._it = it

        def __enter__(self):
            return (_Entry(e) for e in self._it.__enter__())

        def __exit__(self, *a):
            return self._it.__exit__(*a)

    def fake(p=".", *a, **k):
        it = real(p, *a, **k)
        if os.path.normcase(os.path.normpath(os.fspath(p))) == target:
            return _It(it)
        return it
    monkeypatch.setattr(dca.os, "scandir", fake)
    w = _watch(lay, "darwin")
    assert w.poll(T0) is False
    assert w.availability() == dca.UNAVAILABLE


def test_a_refused_subfolder_that_comes_back_is_not_a_run_of_new_blobs(
        tmp_path, monkeypatch):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    w.poll(T0)
    # Churn learned in "2", so a new blob in "1" would light on its own: the
    # strictest place to check that nothing already in "1" reads as new.
    t = _learn_churn(lay, w, sub="2")
    assert w.poll(t + 2) is False
    state = _fail_scandir(monkeypatch, os.path.join(lay.blob_dir, "1"),
                          PermissionError(13, "refused"))
    # Blobs land in "1" while it is refused.
    for i in range(3):
        fx.add_blob(lay.blob_dir, "1", f"gap-{i}")
    assert w.poll(t + 4) is False
    assert w.availability() == dca.UNAVAILABLE
    state["on"] = False
    # Readable again: only recorded, nothing in it is new.
    assert w.poll(t + 6) is False
    assert w.availability() == dca.AVAILABLE
    assert w._candidates == {}
    assert "1" not in w._occasions
    assert w._churn == {"2"}
    assert w.poll(t + 8) is False
    # A blob that really arrives after the gap is still seen.
    fx.add_blob(lay.blob_dir, "1", "after")
    assert w.poll(t + 10) is True


@pytest.mark.parametrize("depth", [("1",), ("1", "00")],
                         ids=["subfolder", "folder-inside-one"])
@pytest.mark.parametrize("exc", [FileNotFoundError(2, "gone"),
                                 NotADirectoryError(20, "gone")],
                         ids=["not-found", "not-a-directory"])
def test_a_nested_blob_folder_that_vanishes_stays_available(
        tmp_path, monkeypatch, depth, exc):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    assert w.poll(T0) is False
    state = _fail_scandir(monkeypatch, os.path.join(lay.blob_dir, *depth),
                          exc)
    # Chromium deleting a folder between the listing and the look inside is
    # the store working, not a store we cannot read.
    assert w.poll(T0 + 2) is False
    assert state["hits"]
    assert w.availability() == dca.AVAILABLE
    names = dca._blob_names(lay.blob_dir)
    assert names is not None
    if depth == ("1",):
        assert "1" not in names          # gone, not empty
    else:
        assert names["1"] == frozenset()
    assert names["2"] == frozenset({"00/1"})


def test_a_refused_is_dir_in_the_top_blob_folder_is_unavailable(
        tmp_path, monkeypatch):
    lay = fx.make_layout(tmp_path)
    real = os.scandir
    target = os.path.normcase(os.path.normpath(lay.blob_dir))

    class _Entry:
        def __init__(self, e):
            self.name = e.name
            self.path = e.path

        def is_dir(self, follow_symlinks=True):
            raise PermissionError(13, "refused")

    class _It:
        def __init__(self, it):
            self._it = it

        def __enter__(self):
            return (_Entry(e) for e in self._it.__enter__())

        def __exit__(self, *a):
            return self._it.__exit__(*a)

    def fake(p=".", *a, **k):
        it = real(p, *a, **k)
        if os.path.normcase(os.path.normpath(os.fspath(p))) == target:
            return _It(it)
        return it
    monkeypatch.setattr(dca.os, "scandir", fake)
    assert dca._blob_names(lay.blob_dir) is None
    w = _watch(lay, "darwin")
    assert w.poll(T0) is False
    assert w.availability() == dca.UNAVAILABLE


def test_a_refused_subfolder_puts_a_lit_light_out_and_the_queue_cannot_resume_it(
        tmp_path, monkeypatch):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    w.poll(T0)
    end = _learn_churn(lay, w, sub="2")
    fx.grow_log(lay.idb_dir, 10)
    fx.add_blob(lay.blob_dir, "1", "reply")
    fx.write_queue(lay.ls_dir, [_ev(dca.SENT, end + 99)])
    assert w.poll(end + 100) is True
    state = _fail_scandir(monkeypatch, os.path.join(lay.blob_dir, "1"),
                          PermissionError(13, "refused"))
    assert w.poll(end + 102) is False
    assert w.availability() == dca.UNAVAILABLE
    state["on"] = False
    # The "sent" is still pending and fresh, but only a write seen between
    # two polls lights it, and the gap threw the baseline away.
    for t in range(104, 300, 4):
        assert w.poll(end + t) is False
    assert w.availability() == dca.AVAILABLE


def test_windows_is_unaffected_by_a_refused_nested_blob_folder(
        tmp_path, monkeypatch):
    lay = fx.make_layout(tmp_path)
    state = _fail_scandir(monkeypatch, os.path.join(lay.blob_dir, "1"),
                          PermissionError(13, "refused"))
    w = _watch(lay, "win32")
    assert w.poll(T0) is False
    fx.grow_log(lay.idb_dir, 10)
    assert w.poll(T0 + 2) is True
    assert w.availability() == dca.AVAILABLE
    assert state["hits"] == 0


# -- nothing written, nothing printed ----------------------------------------

def _snapshot(root):
    out = {}
    for dirpath, dirs, files in os.walk(root):
        for name in dirs + files:
            p = os.path.join(dirpath, name)
            st = os.lstat(p)
            out[p] = (st.st_size, st.st_mtime_ns, st.st_mode)
    return out


@pytest.mark.parametrize("plat", ["win32", "darwin"])
def test_poll_writes_nothing_to_disk(tmp_path, plat):
    lay = fx.make_layout(tmp_path / "desk")
    home = tmp_path / "blink"
    home.mkdir()
    fx.write_queue(lay.ls_dir, [_ev(dca.SENT, T0)])
    w = _watch(lay, plat, blink_home=str(home))
    before = _snapshot(tmp_path)
    for i in range(8):
        w.poll(T0 + 2 * i)
    w.availability()
    assert _snapshot(tmp_path) == before


def test_sentinels_never_reach_output_or_logs(tmp_path, capsys, caplog,
                                              monkeypatch):
    lay = fx.make_layout(tmp_path)
    with open(os.path.join(lay.idb_dir, "000003.log"), "ab") as fh:
        fh.write(SENTINEL)
    fx.add_blob(lay.blob_dir, "1", "SENTINEL-name")
    fx.write_queue(lay.ls_dir, SENTINEL.decode() + "{not json")
    caplog.set_level(0)

    def boom(*a, **k):
        raise RuntimeError(SENTINEL.decode())
    for plat in ("darwin", "win32"):
        w = _watch(lay, plat)
        for i in range(3):
            fx.grow_log(lay.idb_dir, 10)
            w.poll(T0 + 2 * i)
        monkeypatch.setattr(dca, "_newest_log", boom)
        assert w.poll(T0 + 10) is False
        monkeypatch.undo()
    out, err = capsys.readouterr()
    assert out == "" and err == ""
    assert caplog.records == []


# -- the queue only holds, never lights --------------------------------------

def _random_stamp(rng, now):
    """A queue timestamp of every kind the reader accepts or refuses, around
    `now`: seconds, milliseconds, ISO text, far past, far future, junk."""
    import datetime
    t = now + rng.choice([-86400, -5000, -700, -601, -599, -300, -30, -1, 0,
                          5, 30, 59, 61, 3600, 86400 * 10])
    kind = rng.choice(["s", "ms", "iso", "iso", "bad", "none", "bool"])
    if kind == "s":
        return t
    if kind == "ms":
        return t * 1000
    if kind == "iso":
        return datetime.datetime.fromtimestamp(
            t, datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    return {"bad": "tomorrow", "none": None, "bool": True}[kind]


def _random_events(rng, now):
    names = [dca.SENT, dca.SENT, "claudeai.message.perceived_ttlt",
             "claudeai.message.stopped", "claudeai.message.streaming_abandoned",
             "claudeai.other"]
    return [_ev(rng.choice(names), _random_stamp(rng, now))
            for _ in range(rng.randint(0, 5))]


@pytest.mark.parametrize("case", ["win32", "darwin", "darwin-learned"])
def test_no_queue_whatever_it_holds_lights_without_a_write(tmp_path, case):
    """Property: with the IndexedDB quiet, an unlit light never comes on, for
    any "sent" or finish the queue can hold, at any stamp, across clock jumps
    of seconds, minutes and hours (PENDING_CAP_S is 600), and through gaps
    (the folder away for a poll) and an exception inside a poll."""
    rng = random.Random(5201)
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "win32" if case == "win32" else "darwin")
    now = T0
    w.poll(now)
    if case == "darwin-learned":
        now = _learn_churn(lay, w)
    boom = RuntimeError("boom")
    for i in range(400):
        now += rng.choice([2, 2, 2, 30, 301, 700, 5000])
        if rng.random() < 0.6:
            fx.write_queue(lay.ls_dir, _random_events(rng, now))
        if i % 37 == 5:
            moved = lay.idb_dir + ".away"
            os.rename(lay.idb_dir, moved)
            assert w.poll(now) is False
            os.rename(moved, lay.idb_dir)
            continue
        if i % 41 == 9:
            real = w._queue_events
            w._queue_events = lambda *a, **k: (_ for _ in ()).throw(boom)
            assert w.poll(now) is False
            del w._queue_events
            assert w._queue_events == real
            continue
        assert w.poll(now) is False, (case, i)


def test_every_lit_stretch_starts_at_an_indexeddb_write(tmp_path):
    """Property, Windows: whenever the light goes from off to on, the log
    grew at that very poll (or within the hold before it), whatever the queue
    holds; the queue only extends a stretch that is already on."""
    rng = random.Random(5202)
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "win32")
    now = T0
    w.poll(now)
    last_write = None
    prev = False
    baseline = False
    starts = 0
    for i in range(1500):
        now += rng.choice([2, 2, 2, 5, 15, 30, 100, 700])
        if rng.random() < 0.5:
            fx.write_queue(lay.ls_dir, _random_events(rng, now))
        gap = i % 97 == 50
        if gap:
            moved = lay.idb_dir + ".away"
            os.rename(lay.idb_dir, moved)
            assert w.poll(now) is False
            os.rename(moved, lay.idb_dir)
            prev, baseline, last_write = False, True, None
            continue
        if rng.random() < 0.12:
            fx.grow_log(lay.idb_dir, 20)
            if not baseline:
                last_write = now
        got = w.poll(now)
        baseline = False
        if got and not prev:
            starts += 1
            assert last_write is not None
            assert now - last_write <= dca.QUIET_HOLD_S["win32"]
        prev = got
    assert starts > 20                          # the run did light, often


def test_a_far_future_stamp_does_not_extend_a_hold(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "win32")
    w.poll(T0)
    fx.grow_log(lay.idb_dir, 10)
    assert w.poll(T0 + 2) is True
    fx.write_queue(lay.ls_dir, [_ev(dca.SENT, T0 + 10 * 86400)])
    assert w.poll(T0 + 30) is False


@pytest.mark.parametrize("ahead,holds", [(59, True), (61, False)])
def test_a_sent_stamped_ahead_of_our_clock_holds_only_within_the_slack(
        tmp_path, ahead, holds):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "win32")
    w.poll(T0)
    fx.grow_log(lay.idb_dir, 10)
    assert w.poll(T0 + 2) is True
    fx.write_queue(lay.ls_dir, [_ev(dca.SENT, T0 + 30 + ahead)])
    assert w.poll(T0 + 30) is holds


def test_a_sent_older_than_the_cap_does_not_extend_a_fresh_stretch(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "win32")
    far = T0 + dca.PENDING_CAP_S + 100
    fx.write_queue(lay.ls_dir, [_ev(dca.SENT, T0)])    # a lost finish
    w.poll(far)
    fx.grow_log(lay.idb_dir, 10)
    assert w.poll(far + 2) is True
    assert w.poll(far + 22) is True
    assert w.poll(far + 23) is False


def test_the_queue_cannot_resume_a_light_after_an_exception_in_a_poll(
        tmp_path, monkeypatch):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "win32")
    _lit_with_pending(lay, w, T0)
    assert w.poll(T0 + 40) is True              # held by the pending send

    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(dca.DesktopChatActivity, "_pending", boom)
    assert w.poll(T0 + 42) is False
    monkeypatch.undo()
    for t in range(44, 200, 2):
        assert w.poll(T0 + t) is False          # still pending, not lit


def test_the_queue_cannot_resume_a_light_after_the_hold_lapsed_on_macos(
        tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    w.poll(T0)
    end = _learn_churn(lay, w)
    fx.grow_log(lay.idb_dir, 10)
    assert w.poll(end + 100) is True
    assert w.poll(end + 150) is True
    assert w.poll(end + 151) is False
    # The reply's "sent" reaches disk 20-60 s late, after the hold ran out.
    fx.write_queue(lay.ls_dir, [_ev(dca.SENT, end + 99)])
    for t in range(152, 900, 4):
        assert w.poll(end + t) is False


def test_on_macos_a_pending_send_holds_a_lit_light_past_50_s(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    w.poll(T0)
    end = _learn_churn(lay, w)
    fx.write_queue(lay.ls_dir, [_ev(dca.SENT, end + 99)])
    fx.grow_log(lay.idb_dir, 10)
    assert w.poll(end + 100) is True
    assert w.poll(end + 151) is True            # past 50 s, still pending
    assert w.poll(end + 400) is True
    fx.write_queue(lay.ls_dir, [
        _ev(dca.SENT, end + 99),
        _ev("claudeai.message.stopped", end + 401)])
    assert w.poll(end + 402) is False


# -- macOS replies, replayed ------------------------------------------------

def _run_unlearned_replies(tmp_path, scale=1):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    w.poll(T0)
    sim = _Sim(lay, w, T0 + 2, scale=scale)
    end = T0 + 10
    for i, (gap, length, lag, flag, dblob, every) in enumerate(_REPLIES):
        end = sim.reply(end + gap, length, lag, flag, dblob, "1", f"r{i}",
                        blob_every=every)
    sim.run(end + 400)
    return lay, w, sim, end


def test_replies_of_every_shape_never_teach_their_subfolder(tmp_path, capsys):
    """Replies with the queue's "sent" 20-60 s late and the finish up to 60 s
    late, back to back or not, the blob before, with or after the log, more
    blobs in the long ones: ten replies at 2 s polls. The reply's folder is
    never counted, not even once, and the board stays dark (nothing is
    learned, so the IndexedDB says nothing on macOS)."""
    lay, w, sim, _end = _run_unlearned_replies(tmp_path)
    assert "1" not in w._churn
    assert "1" not in w._occasions
    assert w._candidates == {}
    assert not any(sim.answers())
    assert len(sim.on_disk) == 2 * len(_REPLIES)    # the queue saw them all
    out, err = capsys.readouterr()
    assert out == "" and err == ""


def test_after_those_replies_real_churn_is_learned_and_a_reply_lights(
        tmp_path):
    lay, w, sim, end = _run_unlearned_replies(tmp_path)
    # Real churn in another sub-folder, long after the last chat, each time
    # with the log growing in the same tick.
    t = end + 400
    for i in range(3):
        t += 150
        sim.log(t)
        sim.blob(t, "2", f"churn-{i}")
    sim.run(t + dca.IDLE_CONFIRM_S + 10)
    assert w._churn == {"2"}
    assert "1" not in w._churn and "1" not in w._occasions
    assert not any(sim.answers())               # churn never lit it
    # A reply in "1" now lights, and the hold is exactly 50 s from its last
    # write whether the queue sent "sent" or not.
    g = sim.t + 100
    fx.grow_log(lay.idb_dir, 40)
    fx.add_blob(lay.blob_dir, "1", "reply-after")
    assert w.poll(g) is True
    fx.grow_log(lay.idb_dir, 40)
    assert w.poll(g + 10) is True
    base = g + 10
    assert w.poll(base + 50) is True
    assert w.poll(base + 50.01) is False
    # And churn paired with log growth still does not light it.
    fx.grow_log(lay.idb_dir, 40)
    fx.add_blob(lay.blob_dir, "2", "late-churn")
    assert w.poll(base + 400) is False


def test_replies_during_real_churn_learning_in_another_subfolder(tmp_path):
    """A reply every 400 s in "1", idle churn in "2" between them (with the
    log growing in the same tick): "2" is learned on its third occasion, "1"
    never, and from then on each reply lights and each churn write does not."""
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    w.poll(T0)
    sim = _Sim(lay, w, T0 + 2)
    shapes = [(60, 45, 30, 0), (90, 60, 59, -2), (30, 20, 0, 2),
              (120, 35, 20, 0), (50, 55, 45, 6), (80, 25, 10, 0)]
    lit_at = []
    for i, (length, lag, flag, dblob) in enumerate(shapes):
        r = T0 + 10 + 400 * i
        sim.reply(r, length, lag, flag, dblob, "1", f"r{i}")
        sim.log(r + 250)
        sim.blob(r + 250, "2", f"churn-{i}")
        sim.run(r + 399)
        lit_at.append(any(a for t, a in sim.results
                          if r <= t <= r + length + 50))
        assert "1" not in w._churn and "1" not in w._occasions, i
        # The churn write, with its log growth, never lights it.
        assert not any(a for t, a in sim.results if r + 250 <= t <= r + 399)
    assert w._churn == {"2"}
    # Learned on the third churn (seen at r2 + 250, confirmed 90 s later), so
    # the replies from the fourth on light; the first three cannot.
    assert lit_at == [False, False, False, True, True, True]


@pytest.mark.parametrize("scale", [100])
def test_the_replayed_replies_do_not_depend_on_write_sizes(tmp_path, scale):
    _l, _w, small, _e = _run_unlearned_replies(tmp_path / "a", 1)
    _l, _w, big, _e = _run_unlearned_replies(tmp_path / "b", scale)
    assert small.answers() == big.answers()


def test_a_reply_blob_on_an_idle_light_is_a_candidate_not_an_occasion(
        tmp_path):
    """A blob with no chat sign in the window is still counted, once: genuine
    idle churn is learned; this is what the replay must not disturb."""
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    w.poll(T0)
    sim = _Sim(lay, w, T0 + 2)
    for i in range(3):
        sim.blob(T0 + 10 + 200 * i, "1", f"churn-{i}")
    sim.run(T0 + 10 + 400 + dca.IDLE_CONFIRM_S + 2)
    assert w._churn == {"1"}


def test_a_blob_one_poll_before_the_log_pairs_with_it(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    w.poll(T0)
    end = _learn_churn(lay, w)
    fx.add_blob(lay.blob_dir, "1", "early")
    assert w.poll(end + 100) is False
    fx.grow_log(lay.idb_dir, 40)
    assert w.poll(end + 102) is False           # 2 s: within the 3 s window
    fx.add_blob(lay.blob_dir, "1", "early-2")
    assert w.poll(end + 200) is False
    fx.grow_log(lay.idb_dir, 40)
    assert w.poll(end + 204) is True            # 4 s: no longer that churn


# -- a changed set of sub-folders -------------------------------------------

def test_an_added_subfolder_forgets_learning(tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    w.poll(T0)
    end = _learn_churn(lay, w)
    fx.add_blob(lay.blob_dir, "3", "x")
    w.poll(end + 10)
    assert w._churn == set() and w._occasions == {}


def test_a_subfolder_change_while_the_indexeddb_was_unreadable_forgets(
        tmp_path):
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    w.poll(T0)
    end = _learn_churn(lay, w)
    moved = lay.idb_dir + ".away"
    os.rename(lay.idb_dir, moved)
    assert w.poll(end + 10) is False
    assert w.availability() == dca.UNAVAILABLE
    fx.rename_subfolders(lay.blob_dir, {"1": "7"})
    os.rename(moved, lay.idb_dir)
    w.poll(end + 20)
    assert w._churn == set() and w._occasions == {}


def test_a_reset_store_is_a_baseline_not_a_run_of_new_blobs(tmp_path):
    """The renamed folders are not blobs that arrived: counting them would
    start every folder of a reset store at one idle occasion of three."""
    lay = fx.make_layout(tmp_path)
    w = _watch(lay, "darwin")
    w.poll(T0)
    end = _learn_churn(lay, w)
    fx.rename_subfolders(lay.blob_dir, {"1": "7", "2": "8"})
    w.poll(end + 10)
    assert w._candidates == {}
    w.poll(end + 10 + dca.IDLE_CONFIRM_S + 5)
    assert w._occasions == {}


def test_no_file_in_the_indexeddb_folders_is_opened_by_os_open(
        tmp_path, monkeypatch):
    lay = fx.make_layout(tmp_path)
    seen = []
    real = os.open

    def rec(path, *a, **k):
        seen.append(os.fspath(path) if not isinstance(path, int) else "")
        return real(path, *a, **k)
    monkeypatch.setattr(os, "open", rec)
    for plat in ("darwin", "win32"):
        w = _watch(lay, plat)
        w.poll(T0)
        w.poll(T0 + 2)
    monkeypatch.undo()
    idb = os.path.realpath(lay.idb_dir)
    blob = os.path.realpath(lay.blob_dir)
    for p in seen:
        if not p:
            continue
        r = os.path.realpath(p)
        assert not r.startswith(idb) and not r.startswith(blob)
