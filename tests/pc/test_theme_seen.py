"""Theme contract Addenda 4 and 4a: a theme picked on the board while the
daemon was away is kept, not overwritten (review S2, 2026-10-28).

theme_seen.json, written by the daemon alone, holds the last theme the daemon
knew a board had, keyed by board_id. On a boot report from that board:
unchanged -> theme.json may be sent; the save we sent and never heard
confirmed -> our own save landed; anything else -> adopted, nothing sent; no
record, a bad one, or another board's -> theme.json wins, then the record
starts.

Every home is tmp_path (tests/conftest.py makes it ~), and every file here is
written by the test that reads it. A "daemon restart" is a fresh ThemeSync on
the same home: the record lives in theme_seen.json, so it is what carries
over.
"""
import builtins
import json
import os
import time

import pytest

from pc import theme
from tests.support.home import inside

DARKISH = {"id": 12,
           "c": ["0e1116", "e6e8eb", "4387df", "0da243",
                 "0da243", "ba8107", "ff1900"],
           "w": 12, "ends": "round", "dot": 1, "by": "level"}

BOARD = "a1b2c3d4e5f6"
OTHER = "ffeeddccbbaa"


def custom(**over):
    t = json.loads(json.dumps(DARKISH))
    t.update(over)
    return t


def now_msg(tid, why, t=None):
    m = {"t": "theme_now", "v": 2, "id": tid, "why": why}
    if t is not None:
        m["h"] = theme.theme_hash(t)
    return m


def saves(lines):
    return [m for m in lines if m.get("op") == "save"]


def raw_seen(home):
    with open(theme.seen_path(home), encoding="utf-8") as f:
        return json.load(f)


def seen_in(home):
    """The record as the contract spells it: board_id, id and h only."""
    return {k: v for k, v in raw_seen(home).items()
            if k in ("board_id", "id", "h")}


def put_seen(home, obj):
    """theme_seen.json holding `obj` exactly, under the caller's home."""
    path = inside(home, theme.seen_path(home))
    os.makedirs(home, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f)


def connect(home):
    """A daemon start: a new ThemeSync, greeted."""
    s = theme.ThemeSync(home)
    s.hello()
    return s


def confirmed(home, tid, t=None, board=BOARD):
    """The board confirmed storing `tid` while a daemon was up."""
    theme.ThemeSync(home).board_now(now_msg(tid, "saved", t), board_id=board)


@pytest.fixture
def home(tmp_path):
    return str(tmp_path / ".blink")


# ------------------------------------------------------------- the rules

def test_the_reviewers_reproduction_keeps_the_boards_choice(home):
    # Review S2, step by step: `blink theme set ocean`, the board answers
    # saved; the daemon goes away; Neon is picked on the board; on reconnect
    # the board reports boot 1. Before Addendum 4 the daemon sent save 4.
    theme.save(home, {"id": 4})
    s = connect(home)
    s.board_now(now_msg(0, "boot"), board_id=BOARD)
    assert saves(s.outgoing()) == [theme.message("save", {"id": 4})]
    s.board_now(now_msg(4, "saved"), board_id=BOARD)
    assert seen_in(home) == {"id": 4, "board_id": BOARD}
    s = connect(home)                                   # daemon restarted
    s.board_now(now_msg(1, "boot"), board_id=BOARD)
    assert s.outgoing() == []
    assert theme.load_saved(home) == {"id": 1}
    assert seen_in(home) == {"id": 1, "board_id": BOARD}


def test_board_unchanged_and_computer_changed_sends_a_save(home):
    theme.save(home, {"id": 4})
    confirmed(home, 4)
    theme.save(home, {"id": 7})          # `blink theme set` while away
    s = connect(home)
    s.board_now(now_msg(4, "boot"), board_id=BOARD)
    assert saves(s.outgoing()) == [theme.message("save", {"id": 7})]
    assert theme.load_saved(home) == {"id": 7}
    assert s.outgoing() == []


def test_neither_changed_sends_nothing(home):
    theme.save(home, {"id": 4})
    confirmed(home, 4)
    s = connect(home)
    s.board_now(now_msg(4, "boot"), board_id=BOARD)
    assert s.outgoing() == []
    assert theme.load_saved(home) == {"id": 4}


def test_both_changed_the_board_wins_and_says_so(home):
    theme.save(home, {"id": 4})
    confirmed(home, 4)
    theme.save(home, {"id": 7})                        # this computer
    s = connect(home)
    s.board_now(now_msg(1, "boot"), board_id=BOARD)    # and the board
    assert s.outgoing() == []
    assert theme.load_saved(home) == {"id": 1}
    assert theme.board_kept(home) == ({"id": 1}, {"id": 7})
    # Moot once a theme is set here again.
    theme.save(home, {"id": 7})
    assert theme.board_kept(home) is None


def test_a_change_on_the_board_alone_is_not_a_conflict(home):
    theme.save(home, {"id": 4})
    confirmed(home, 4)
    s = connect(home)
    s.board_now(now_msg(1, "boot"), board_id=BOARD)
    assert theme.load_saved(home) == {"id": 1}
    assert theme.board_kept(home) is None


def test_an_adopted_built_in_keeps_the_custom_colours(home):
    theme.save(home, DARKISH)
    confirmed(home, 12, DARKISH)
    s = connect(home)
    s.board_now(now_msg(5, "boot"), board_id=BOARD)
    assert theme.load_saved(home) == {"id": 5}
    assert theme.saved_custom(home) == DARKISH
    assert s.outgoing() == []


# ------------------------------------------------------------- N1

def test_our_own_save_landing_unconfirmed_is_not_the_boards_choice(home):
    # Review of fp/fix-pc, N1, reproduced: seen Dark; `set ocean` goes out;
    # the board stores it but the saved line is lost; with the daemon down,
    # `set retro-lcd`. On restart the board reports Ocean -- our own save,
    # not the board's choice -- so Retro LCD must still be sent.
    theme.save(home, {"id": 0})
    confirmed(home, 0)
    theme.save(home, {"id": 4})
    s = connect(home)
    s.board_now(now_msg(0, "boot"), board_id=BOARD)
    assert saves(s.outgoing()) == [theme.message("save", {"id": 4})]
    assert raw_seen(home)["asked"] == {"id": 4}
    theme.save(home, {"id": 7})          # daemon down; the saved line lost
    s = connect(home)
    s.board_now(now_msg(4, "boot"), board_id=BOARD)
    assert theme.load_saved(home) == {"id": 7}
    assert saves(s.outgoing()) == [theme.message("save", {"id": 7})]
    assert seen_in(home) == {"id": 4, "board_id": BOARD}


def test_a_confirmation_clears_the_unconfirmed_save(home):
    theme.save(home, {"id": 4})
    s = connect(home)
    s.board_now(now_msg(0, "boot"), board_id=BOARD)
    s.outgoing()
    assert raw_seen(home)["asked"] == {"id": 4}
    s.board_now(now_msg(4, "saved"), board_id=BOARD)
    assert "asked" not in raw_seen(home)


def test_a_save_before_the_board_is_named_is_not_recorded(home):
    theme.save(home, {"id": 4})
    s = connect(home)
    s.board_now(now_msg(0, "boot"))
    assert saves(s.outgoing()) == [theme.message("save", {"id": 4})]
    assert not os.path.exists(theme.seen_path(home))


# ------------------------------------------------------------- no record

def test_no_record_keeps_todays_rule_and_starts_one(home):
    theme.save(home, {"id": 4})
    s = connect(home)
    s.board_now(now_msg(1, "boot"), board_id=BOARD)
    assert saves(s.outgoing()) == [theme.message("save", {"id": 4})]
    assert theme.load_saved(home) == {"id": 4}
    assert seen_in(home) == {"id": 1, "board_id": BOARD}


def test_seen_never_touches_theme_json(home):
    # Addendum 4a: theme.json is the CLI's and the builder's; a report from
    # the board writes only theme_seen.json.
    s = connect(home)
    s.board_now(now_msg(6, "boot"), board_id=BOARD)
    s.board_now(now_msg(6, "saved"), board_id=BOARD)
    assert s.outgoing() == []
    assert not os.path.exists(theme.theme_path(home))
    assert seen_in(home) == {"id": 6, "board_id": BOARD}
    # A later change on the board, while away, becomes this computer's.
    s = connect(home)
    s.board_now(now_msg(2, "boot"), board_id=BOARD)
    assert theme.load_saved(home) == {"id": 2}
    assert s.outgoing() == []


@pytest.mark.parametrize("bad", [
    "not a dict",
    None,
    [],
    {"id": 1},                                         # no board
    {"board_id": "", "id": 1},                         # empty board
    {"board_id": 7, "id": 1},                          # not a string
    {"board_id": BOARD},                               # no id
    {"board_id": BOARD, "id": 99},                     # not a theme
    {"board_id": BOARD, "id": True},
    {"board_id": BOARD, "id": 12},                     # custom, no h
    {"board_id": BOARD, "id": 12, "h": "XYZ!"},        # custom, bad h
])
def test_a_corrupt_record_is_no_record(home, bad):
    theme.save(home, {"id": 4})
    put_seen(home, bad)
    assert theme.load_seen(home) is None
    s = connect(home)
    s.board_now(now_msg(1, "boot"), board_id=BOARD)
    assert theme.load_saved(home) == {"id": 4}
    assert saves(s.outgoing()) == [theme.message("save", {"id": 4})]
    assert seen_in(home) == {"id": 1, "board_id": BOARD}


def test_a_record_file_that_does_not_parse_is_no_record(home):
    os.makedirs(home)
    with open(inside(home, theme.seen_path(home)), "w",
              encoding="utf-8") as f:
        f.write('{"board_id": "a1"')
    theme.save(home, {"id": 4})
    s = connect(home)
    s.board_now(now_msg(1, "boot"), board_id=BOARD)
    assert theme.load_saved(home) == {"id": 4}
    assert theme.load_seen(home)["fp"] == {"id": 1}


def test_another_boards_record_keeps_todays_rule(home):
    theme.save(home, {"id": 4})
    confirmed(home, 4)
    s = connect(home)
    s.board_now(now_msg(9, "boot"), board_id=OTHER)
    assert theme.load_saved(home) == {"id": 4}
    assert saves(s.outgoing()) == [theme.message("save", {"id": 4})]
    assert seen_in(home) == {"id": 9, "board_id": OTHER}
    # And from then on that board is the one compared.
    s = connect(home)
    s.board_now(now_msg(3, "boot"), board_id=OTHER)
    assert theme.load_saved(home) == {"id": 3}


@pytest.mark.parametrize("unknown", [None, "", 5])
def test_with_no_board_id_nothing_is_compared_or_recorded(home, unknown):
    theme.save(home, {"id": 4})
    confirmed(home, 4)
    s = connect(home)
    s.board_now(now_msg(1, "boot"), board_id=unknown)
    assert theme.load_saved(home) == {"id": 4}
    assert saves(s.outgoing()) == [theme.message("save", {"id": 4})]
    assert seen_in(home) == {"id": 4, "board_id": BOARD}


# ------------------------------------------------------------- unreadable

def test_an_unreadable_theme_json_is_never_written_over(home, monkeypatch,
                                                        capsys):
    # Review of fp/fix-pc, S2: an antivirus or backup tool holding the file
    # read as "empty", and the write after it replaced the owner's theme.
    theme.save(home, DARKISH)
    confirmed(home, 12, DARKISH)
    before = open(theme.theme_path(home), "rb").read()
    real_open = builtins.open

    def locked(path, *a, **k):
        if os.path.basename(str(path)) == "theme.json":
            raise PermissionError(13, "held by another process")
        return real_open(path, *a, **k)
    monkeypatch.setattr(builtins, "open", locked)
    s = connect(home)
    s.board_now(now_msg(5, "boot"), board_id=BOARD)
    with pytest.raises(theme.Unreadable):
        theme.save(home, {"id": 2})
    with pytest.raises(theme.Unreadable):
        theme.forget(home)
    monkeypatch.setattr(builtins, "open", real_open)
    assert open(theme.theme_path(home), "rb").read() == before
    err = capsys.readouterr().err
    assert "could not keep the board's choice" in err
    assert "PermissionError" in err


def test_a_theme_json_that_does_not_parse_is_left_alone(home, capsys):
    os.makedirs(home)
    path = inside(home, theme.theme_path(home))
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"id": 4, "secret-ish": "owner text"')
    confirmed(home, 4)
    s = connect(home)
    s.board_now(now_msg(1, "boot"), board_id=BOARD)
    assert open(path, encoding="utf-8").read() == (
        '{"id": 4, "secret-ish": "owner text"')
    err = capsys.readouterr().err
    assert "could not be read" in err
    assert "owner text" not in err


def _fail_theme_json_reads(monkeypatch, times):
    """The next `times` opens of theme.json raise PermissionError, as an
    antivirus scan or a backup tool holding it would."""
    real_open = builtins.open
    left = [times]

    def flaky(path, *a, **k):
        if os.path.basename(str(path)) == "theme.json" and left[0] > 0:
            left[0] -= 1
            raise PermissionError(13, "held by another process")
        return real_open(path, *a, **k)
    monkeypatch.setattr(builtins, "open", flaky)
    return left


@pytest.mark.parametrize("why", ["user", "boot"])
def test_a_refused_adopt_is_retried_and_never_turns_into_a_save(
        home, monkeypatch, capsys, why):
    # Round-2 review, R1, reproduced: theme.json Ocean; Neon picked on the
    # board while the file cannot be read. The adopt is refused, rightly,
    # and the next tick read Ocean again and sent it, reverting the panel.
    theme.save(home, {"id": 4})
    confirmed(home, 4)
    s = connect(home)
    s.board_now(now_msg(4, "boot"), board_id=BOARD)
    # Unreadable for the length of the report, readable again after it.
    left = _fail_theme_json_reads(monkeypatch, 10 ** 6)
    s.board_now(now_msg(1, why), board_id=BOARD)
    left[0] = 0
    assert theme.load_saved(home) == {"id": 4}      # refused, not written
    assert s.outgoing() == []                       # retried: goes through
    assert theme.load_saved(home) == {"id": 1}
    assert s.outgoing() == []
    assert capsys.readouterr().err.count("could not keep") == 1


def test_a_file_that_stays_unreadable_sends_nothing(home, monkeypatch,
                                                     capsys):
    theme.save(home, {"id": 4})
    s = connect(home)
    s.board_now(now_msg(4, "boot"), board_id=BOARD)
    _fail_theme_json_reads(monkeypatch, 10 ** 6)
    s.board_now(now_msg(1, "user"), board_id=BOARD)
    for _ in range(3):
        assert s.outgoing() == []
    # Logged once, not once per tick.
    assert capsys.readouterr().err.count("could not keep") == 1


def test_a_pending_adopt_ends_on_a_hello_or_the_next_report(home,
                                                            monkeypatch):
    theme.save(home, {"id": 4})
    s = connect(home)
    s.board_now(now_msg(4, "boot"), board_id=BOARD)
    left = _fail_theme_json_reads(monkeypatch, 10 ** 6)
    s.board_now(now_msg(1, "user"), board_id=BOARD)
    left[0] = 0
    s.hello()                            # the board rebooted: it says again
    s.board_now(now_msg(4, "boot"))      # and holds Ocean after all
    assert saves(s.outgoing()) == []
    assert theme.load_saved(home) == {"id": 4}
    left[0] = 10 ** 6
    s.board_now(now_msg(1, "user"), board_id=BOARD)
    left[0] = 0
    s.board_now(now_msg(6, "user"), board_id=BOARD)  # a newer pick decides
    assert s.outgoing() == []
    assert theme.load_saved(home) == {"id": 6}


class _HeldThemeJson:
    """os.replace onto theme.json refused with WinError 5, as a read-only or
    held file on a Windows desk gives (finding 14's scenario), on any
    runner; and every sleep recorded instead of slept."""

    def __init__(self, monkeypatch):
        self.on = True
        self.attempts = 0
        self.sleeps = []
        real = os.replace

        def replace(src, dst):
            if self.on and os.path.basename(str(dst)) == "theme.json":
                self.attempts += 1
                e = PermissionError(13, "Access is denied")
                e.winerror = 5
                raise e
            return real(src, dst)
        monkeypatch.setattr(os, "replace", replace)
        monkeypatch.setattr(time, "sleep", self.sleeps.append)


def test_a_refused_adopt_write_never_sleeps_and_backs_off(home, monkeypatch,
                                                          capsys):
    """The serial thread reaches theme.json through a pending adopt on every
    fast tick. With _write_json's default ten tries it slept 2.7 s each
    time, holding pongs and usage lines (2026-11-01 third review). One try
    from the serial thread, and a refused write retried on a backoff."""
    theme.save(home, {"id": 4})
    clock = [1000.0]
    s = theme.ThemeSync(home, clock=lambda: clock[0])
    s.hello()
    s.board_now(now_msg(4, "boot"), board_id=BOARD)
    held = _HeldThemeJson(monkeypatch)
    s.board_now(now_msg(1, "user"), board_id=BOARD)
    assert held.attempts == 1
    for _ in range(5):                   # fast ticks, no time passing
        assert s.outgoing() == []
    assert held.attempts == 1
    attempts = []
    for _ in range(40):                  # half a minute of half-second ticks
        clock[0] += 0.5
        s.outgoing()
        attempts.append(held.attempts)
    assert 1 < held.attempts <= 6, attempts
    assert held.sleeps == []
    held.on = False
    clock[0] += theme.ThemeSync.ADOPT_RETRY_MAX_S
    assert s.outgoing() == []
    assert theme.load_saved(home) == {"id": 1}
    assert capsys.readouterr().err.count("could not keep") == 1
    assert held.sleeps == []


def test_a_new_refusal_is_tried_at_once_and_the_backoff_starts_over(
        home, monkeypatch):
    theme.save(home, {"id": 4})
    clock = [1000.0]
    s = theme.ThemeSync(home, clock=lambda: clock[0])
    s.hello()
    s.board_now(now_msg(4, "boot"), board_id=BOARD)
    held = _HeldThemeJson(monkeypatch)
    s.board_now(now_msg(1, "user"), board_id=BOARD)
    for _ in range(200):
        clock[0] += 0.5
        s.outgoing()
    before = held.attempts
    s.board_now(now_msg(6, "user"), board_id=BOARD)     # a newer pick
    assert held.attempts == before + 1
    clock[0] += theme.ThemeSync.ADOPT_RETRY_FIRST_S
    s.outgoing()
    assert held.attempts == before + 2


def test_the_serial_threads_record_writes_take_one_try(home, monkeypatch):
    """theme_sent.json and theme_seen.json are written from board_now, on
    the serial thread, too."""
    seen = []
    real = theme._write_json

    def spy(path, obj, tries=None):
        seen.append((os.path.basename(path), tries))
        return real(path, obj, tries=tries)
    monkeypatch.setattr(theme, "_write_json", spy)
    s = theme.ThemeSync(home)
    s.hello()
    s.board_now(now_msg(4, "boot"), board_id=BOARD)
    s.board_now(now_msg(1, "user"), board_id=BOARD)
    s.board_now(now_msg(1, "saved"), board_id=BOARD)
    assert {n for n, _ in seen} >= {"theme.json", "theme_seen.json",
                                    "theme_sent.json"}
    assert all(t == 1 for _, t in seen), seen


class _HeldRecord:
    """os.replace onto theme_seen.json refused with WinError 32, an
    antivirus or the indexer holding the fresh file on a Windows desk, on
    any runner; and every sleep recorded instead of slept."""

    def __init__(self, monkeypatch, on=True):
        self.on = on
        self.attempts = 0
        self.sleeps = []
        real = os.replace

        def replace(src, dst):
            if self.on and os.path.basename(str(dst)) == theme.SEEN_FILE:
                self.attempts += 1
                e = PermissionError(13, "The process cannot access the file")
                e.winerror = 32
                raise e
            return real(src, dst)
        monkeypatch.setattr(os, "replace", replace)
        monkeypatch.setattr(time, "sleep", self.sleeps.append)


def _clocked(home):
    clock = [1000.0]
    s = theme.ThemeSync(home, clock=lambda: clock[0])
    s.hello()
    return s, clock


def test_a_refused_record_of_a_board_pick_lands_once_the_hold_clears(
        home, monkeypatch, capsys):
    """Fourth review, 2026-11-01: with one try and no retry, a single
    refusal left theme_seen.json at Neon after the owner picked Ocean on
    the board, for good."""
    theme.save(home, {"id": 1})
    s, clock = _clocked(home)
    s.board_now(now_msg(1, "boot"), board_id=BOARD)
    held = _HeldRecord(monkeypatch)
    s.board_now(now_msg(4, "user"), board_id=BOARD)
    assert theme.load_saved(home) == {"id": 4}
    assert theme.load_seen(home)["fp"] == {"id": 1}        # refused
    for _ in range(5):                   # fast ticks, no time passing
        assert s.outgoing() == []
    assert held.attempts == 1            # backing off, not every tick
    for _ in range(40):                  # half a minute of half-second ticks
        clock[0] += 0.5
        assert s.outgoing() == []
    assert 1 < held.attempts <= 6
    held.on = False
    clock[0] += theme.ThemeSync.ADOPT_RETRY_MAX_S
    assert s.outgoing() == []
    assert theme.load_seen(home)["fp"] == {"id": 4}
    assert held.sleeps == []
    assert capsys.readouterr().err.count("could not record") == 1


def test_the_reviews_ocean_then_forest_ends_on_forest(home, monkeypatch):
    """The fourth review's scenario: record Neon; Ocean picked on the board
    with the record's write refused once; unplugged; `blink theme set`
    Forest; plugged back in with a boot report of Ocean. Ocean is what was
    last seen on that board, so Forest is this computer's change and goes
    out. With the record left at Neon, Ocean was adopted over Forest."""
    NEON, OCEAN, FOREST = 1, 4, 7
    theme.save(home, {"id": NEON})
    s, clock = _clocked(home)
    s.board_now(now_msg(NEON, "boot"), board_id=BOARD)
    held = _HeldRecord(monkeypatch)
    s.board_now(now_msg(OCEAN, "user"), board_id=BOARD)
    held.on = False                      # the hold clears
    for _ in range(10):
        clock[0] += 0.5
        s.outgoing()
    assert theme.load_seen(home)["fp"] == {"id": OCEAN}
    theme.save(home, {"id": FOREST})     # unplugged: set on the computer
    s.hello()
    s.board_now(now_msg(OCEAN, "boot"), board_id=BOARD)
    assert saves(s.outgoing()) == [theme.message("save", {"id": FOREST})]
    assert theme.load_saved(home) == {"id": FOREST}
    assert theme.board_kept(home) is None
    assert held.sleeps == []


def test_a_record_still_refused_at_the_replug_is_judged_as_written(
        home, monkeypatch):
    """The hold outlasts the unplug: the record this daemon owes is the one
    it compares against, not the stale file."""
    theme.save(home, {"id": 1})
    s, clock = _clocked(home)
    s.board_now(now_msg(1, "boot"), board_id=BOARD)
    held = _HeldRecord(monkeypatch)
    s.board_now(now_msg(4, "user"), board_id=BOARD)
    theme.save(home, {"id": 7})
    s.hello()
    s.board_now(now_msg(4, "boot"), board_id=BOARD)
    assert saves(s.outgoing()) == [theme.message("save", {"id": 7})]
    assert theme.load_saved(home) == {"id": 7}
    held.on = False
    clock[0] += theme.ThemeSync.ADOPT_RETRY_MAX_S
    s.outgoing()
    rec = theme.load_seen(home)
    assert rec["fp"] == {"id": 4} and rec["asked"] == {"id": 7}


def test_a_refused_record_of_the_save_sent_lands_and_keeps_n1(
        home, monkeypatch):
    """_note_asked refused once: `asked` was lost, and after a restart the
    board's boot report of our own earlier save was adopted as its choice
    over the computer's later one (N1)."""
    theme.save(home, {"id": 1})
    s, clock = _clocked(home)
    s.board_now(now_msg(1, "boot"), board_id=BOARD)
    theme.save(home, {"id": 4})
    held = _HeldRecord(monkeypatch)
    assert saves(s.outgoing()) == [theme.message("save", {"id": 4})]
    assert theme.load_seen(home)["asked"] is None          # refused
    held.on = False
    clock[0] += theme.ThemeSync.ADOPT_RETRY_FIRST_S
    assert saves(s.outgoing()) == []
    assert theme.load_seen(home)["asked"] == {"id": 4}
    # The save landed but its why=saved was lost; the daemon restarts after
    # the owner sets another theme here.
    theme.save(home, {"id": 7})
    s = connect(home)
    s.board_now(now_msg(4, "boot"), board_id=BOARD)
    assert saves(s.outgoing()) == [theme.message("save", {"id": 7})]
    assert theme.load_saved(home) == {"id": 7}
    assert held.sleeps == []


def test_a_newer_record_supersedes_an_older_pending_one(home, monkeypatch):
    theme.save(home, {"id": 1})
    s, clock = _clocked(home)
    s.board_now(now_msg(1, "boot"), board_id=BOARD)
    held = _HeldRecord(monkeypatch)
    s.board_now(now_msg(4, "user"), board_id=BOARD)
    s.board_now(now_msg(6, "user"), board_id=BOARD)        # refused too
    assert held.attempts == 2            # a new record is tried at once
    held.on = False
    clock[0] += theme.ThemeSync.ADOPT_RETRY_MAX_S
    s.outgoing()
    assert theme.load_seen(home)["fp"] == {"id": 6}
    for _ in range(5):                   # and the older one never follows
        clock[0] += theme.ThemeSync.ADOPT_RETRY_MAX_S
        s.outgoing()
    assert theme.load_seen(home)["fp"] == {"id": 6}
    assert theme.load_saved(home) == {"id": 6}


def test_a_record_that_lands_on_the_next_write_is_no_longer_owed(
        home, monkeypatch):
    theme.save(home, {"id": 1})
    s, clock = _clocked(home)
    s.board_now(now_msg(1, "boot"), board_id=BOARD)
    held = _HeldRecord(monkeypatch)
    s.board_now(now_msg(4, "user"), board_id=BOARD)
    held.on = False
    s.board_now(now_msg(6, "user"), board_id=BOARD)        # lands at once
    assert theme.load_seen(home)["fp"] == {"id": 6}
    before = held.attempts
    held.on = True
    for _ in range(5):
        clock[0] += theme.ThemeSync.ADOPT_RETRY_MAX_S
        s.outgoing()
    assert held.attempts == before       # nothing left to retry


def test_a_new_pick_on_the_board_clears_the_kept_note(home):
    # Round-2 review, R4: the note was carried past a why=user, so picking
    # the kept theme again later brought a stale "kept over" back.
    theme.save(home, {"id": 4})
    confirmed(home, 4)
    theme.save(home, {"id": 7})
    s = connect(home)
    s.board_now(now_msg(1, "boot"), board_id=BOARD)
    assert theme.board_kept(home) == ({"id": 1}, {"id": 7})
    s.board_now(now_msg(2, "user"), board_id=BOARD)
    s.board_now(now_msg(1, "user"), board_id=BOARD)
    assert theme.load_saved(home) == {"id": 1}
    assert theme.board_kept(home) is None


def test_only_a_missing_theme_json_reads_as_empty(home):
    theme.forget(home)                   # no file: nothing to do, no error
    theme.save(home, {"id": 3})
    assert theme.load_saved(home) == {"id": 3}


# ------------------------------------------------------------- custom, by h

def test_a_custom_theme_is_compared_by_its_hash(home):
    theme.save(home, DARKISH)
    confirmed(home, 12, DARKISH)
    assert seen_in(home) == {"id": 12, "h": theme.theme_hash(DARKISH),
                             "board_id": BOARD}
    mine = custom(dot=0)
    theme.save(home, mine)
    s = connect(home)
    s.board_now(now_msg(12, "boot", DARKISH), board_id=BOARD)
    assert saves(s.outgoing()) == [theme.message("save", mine)]


def test_a_different_custom_on_the_board_is_a_change(home):
    theme.save(home, DARKISH)
    confirmed(home, 12, DARKISH)
    elsewhere = custom(w=22)
    s = connect(home)
    s.board_now(now_msg(12, "boot", elsewhere), board_id=BOARD)
    assert s.outgoing() == []
    assert theme.load_saved(home) is None
    assert seen_in(home) == {"id": 12, "h": theme.theme_hash(elsewhere),
                             "board_id": BOARD}


def test_back_to_our_custom_from_a_built_in_is_adopted(home):
    theme.save(home, {"id": 1}, keep_custom=DARKISH)
    confirmed(home, 1)
    s = connect(home)
    s.board_now(now_msg(12, "boot", DARKISH), board_id=BOARD)
    assert theme.load_saved(home) == DARKISH
    assert s.outgoing() == []


def test_a_custom_report_without_a_hash_drops_the_record(home):
    theme.save(home, {"id": 4})
    confirmed(home, 4)
    s = connect(home)
    s.board_now({"t": "theme_now", "v": 2, "id": 12, "why": "boot"},
                board_id=BOARD)
    assert theme.load_saved(home) == {"id": 4}
    assert saves(s.outgoing()) == [theme.message("save", {"id": 4})]
    assert not os.path.exists(theme.seen_path(home))


# ------------------------------------------------------------- the record

def test_seen_follows_every_why(home):
    s = connect(home)
    for tid, why in ((3, "boot"), (5, "user"), (8, "saved"), (2, "sparkle")):
        s.board_now(now_msg(tid, why), board_id=BOARD)
        assert seen_in(home) == {"id": tid, "board_id": BOARD}


def test_an_unknown_why_is_not_adopted_even_when_it_differs(home):
    theme.save(home, {"id": 4})
    confirmed(home, 4)
    s = connect(home)
    s.board_now(now_msg(1, "sparkle"), board_id=BOARD)
    assert theme.load_saved(home) == {"id": 4}


def test_the_second_boot_report_of_one_connect_agrees(home):
    theme.save(home, {"id": 4})
    confirmed(home, 4)
    s = connect(home)
    s.board_now(now_msg(1, "boot"), board_id=BOARD)
    s.board_now(now_msg(1, "boot"), board_id=BOARD)
    assert theme.load_saved(home) == {"id": 1}
    assert s.outgoing() == []


def test_an_unconfirmed_save_lost_to_a_reboot_is_sent_again(home):
    theme.save(home, {"id": 4})
    s = connect(home)
    s.board_now(now_msg(0, "boot"), board_id=BOARD)
    assert len(saves(s.outgoing())) == 1
    s.hello()
    s.board_now(now_msg(0, "boot"), board_id=BOARD)
    assert saves(s.outgoing()) == [theme.message("save", {"id": 4})]
    assert theme.load_saved(home) == {"id": 4}


def test_seen_is_written_only_when_it_changes(home, monkeypatch):
    s = connect(home)
    s.board_now(now_msg(3, "boot"), board_id=BOARD)
    writes = []
    real = theme._write_json
    monkeypatch.setattr(theme, "_write_json",
                        lambda p, o: (writes.append(p), real(p, o)))
    for _ in range(4):
        s.board_now(now_msg(3, "boot"), board_id=BOARD)
    assert writes == []


def test_save_and_forget_keep_unknown_keys(home):
    os.makedirs(home)
    with open(inside(home, theme.theme_path(home)), "w",
              encoding="utf-8") as f:
        json.dump({"id": 3, "later": {"from": "a newer daemon"}}, f)
    theme.save(home, DARKISH)
    theme.save(home, {"id": 2})          # the custom fields go
    with open(theme.theme_path(home), encoding="utf-8") as f:
        assert json.load(f) == {"id": 2, "later": {"from": "a newer daemon"}}
    theme.forget(home)
    assert theme.load_saved(home) is None
    with open(theme.theme_path(home), encoding="utf-8") as f:
        assert json.load(f) == {"later": {"from": "a newer daemon"}}


def test_a_failed_record_write_is_reported_and_the_sync_goes_on(
        home, monkeypatch, capsys):
    theme.save(home, {"id": 4})
    s = connect(home)

    def refuse(p, o, tries=None):
        raise PermissionError(13, "denied")
    monkeypatch.setattr(theme, "_write_json", refuse)
    s.board_now(now_msg(1, "boot"), board_id=BOARD)
    assert "could not record" in capsys.readouterr().err
    assert saves(s.outgoing()) == [theme.message("save", {"id": 4})]


def test_the_adoption_line_carries_no_value_off_the_wire(home, capsys):
    theme.save(home, {"id": 4})
    confirmed(home, 4)
    capsys.readouterr()
    s = connect(home)
    s.board_now(now_msg(11, "boot"), board_id=BOARD)
    err = capsys.readouterr().err
    assert "changed on the board" in err
    assert "11" not in err and BOARD not in err
    # A whole sentence: it ended "keeping the board's" (audit S5).
    assert "keeping the board's choice" in err
