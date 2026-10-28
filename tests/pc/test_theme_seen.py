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

    def refuse(p, o):
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
