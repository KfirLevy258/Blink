"""Custom themes on the PC side: the model, the files, and the daemon's sync.

Everything here works under tmp_path (tests/conftest.py makes it ~), and every
fixture is built by the test that uses it.
"""
import json
import os

import pytest

from pc import protocol
from pc import theme


DARKISH = {"id": 12,
           "c": ["0e1116", "e6e8eb", "4387df", "0da243",
                 "0da243", "ba8107", "ff1900"],
           "w": 12, "ends": "round", "dot": 1, "by": "level"}


def custom(**over):
    t = json.loads(json.dumps(DARKISH))
    t.update(over)
    return t


# ------------------------------------------------------------- validation

def test_a_built_in_theme_is_just_its_id():
    assert theme.validate({"id": 3}) == {"id": 3}
    assert theme.validate({"id": 0, "c": ["ffffff"] * 7}) == {"id": 0}


def test_every_contract_id_has_a_name_in_order():
    assert theme.NAMES[:13] == (
        "dark", "neon", "bold", "mono", "ocean", "synthwave", "forest",
        "retro_lcd", "cvd_safe", "light", "light_soft", "light_ink",
        "custom")
    assert theme.CUSTOM == 12


@pytest.mark.parametrize("bad", [-1, 13, 99, "3", 3.0, True, None])
def test_an_unknown_id_is_refused(bad):
    with pytest.raises(theme.ThemeError):
        theme.validate({"id": bad})


def test_a_custom_theme_is_normalised_to_lower_case():
    t = custom(c=["0E1116", "E6E8EB", "4387DF", "0DA243",
                  "0DA243", "BA8107", "FF1900"])
    assert theme.validate(t) == DARKISH


@pytest.mark.parametrize("colours", [
    ["0e1116"] * 6,                                  # one short
    ["0e1116"] * 8,                                  # one over
    DARKISH["c"][:6] + ["ff190"],                    # five digits
    DARKISH["c"][:6] + ["#ff1900"],                  # a hash on the wire
    DARKISH["c"][:6] + ["gg1900"],                   # not hex
    DARKISH["c"][:6] + [0xff1900],                   # a number
    "0e1116",                                        # not a list
])
def test_bad_colours_are_refused(colours):
    with pytest.raises(theme.ThemeError):
        theme.validate(custom(c=colours))


@pytest.mark.parametrize("field,value", [
    ("w", 8), ("w", "12"), ("ends", "square"), ("dot", 2), ("dot", "on"),
    ("by", "both"),
])
def test_bad_style_fields_are_refused(field, value):
    with pytest.raises(theme.ThemeError):
        theme.validate(custom(**{field: value}))


def test_a_missing_custom_field_is_refused():
    t = custom()
    del t["ends"]
    with pytest.raises(theme.ThemeError):
        theme.validate(t)


def test_unreadable_text_is_refused_before_the_board_sees_it():
    # Grey on grey: 1.3:1. The plan's floor is 3:1.
    t = custom(c=["777777", "999999"] + DARKISH["c"][2:])
    with pytest.raises(theme.ThemeError, match="3.05:1"):
        theme.validate(t)


def test_contrast_is_the_wcag_ratio():
    assert theme.contrast("000000", "ffffff") == pytest.approx(21.0)
    assert theme.contrast("ffffff", "ffffff") == pytest.approx(1.0)


def test_a_non_object_is_refused():
    for bad in (None, [], "dark", 3):
        with pytest.raises(theme.ThemeError):
            theme.validate(bad)


# ------------------------------------------------------------- the wire

def test_a_preview_line_for_a_built_in_carries_only_the_id():
    m = theme.message("preview", {"id": 5})
    assert m == {"t": "theme", "v": protocol.VERSION, "op": "preview",
                 "id": 5}


def test_a_save_line_for_custom_carries_every_field():
    m = theme.message("save", DARKISH)
    assert m["op"] == "save" and m["id"] == 12
    assert m["c"] == DARKISH["c"]
    assert (m["w"], m["ends"], m["dot"], m["by"]) == (12, "round", 1,
                                                      "level")


def test_cancel_carries_no_theme():
    m = theme.message("cancel")
    assert m == {"t": "theme", "v": protocol.VERSION, "op": "cancel"}


def test_the_widest_theme_line_fits_the_board_with_room():
    raw, why = protocol.encode_checked(theme.message("preview", custom(
        ends="round", by="level", w=22)))
    assert why is None and len(raw) < 200


def test_message_refuses_a_bad_op_and_a_bad_theme():
    with pytest.raises(theme.ThemeError):
        theme.message("store", {"id": 1})
    with pytest.raises(theme.ThemeError):
        theme.message("save", {"id": 40})


def test_message_refuses_a_line_the_board_would_drop(monkeypatch):
    monkeypatch.setattr(protocol, "MAX_LINE_BYTES", 40)
    with pytest.raises(theme.ThemeError, match="byte"):
        theme.message("save", DARKISH)


# The gate on theme lines is the board's "theme" cap now, not a version:
# tests/pc/test_board_caps.py.


def test_welcome_advertises_the_theme_capability():
    assert "theme" in protocol.welcome("blink-bridge", "1.4.0")["caps"]


# ------------------------------------------------------------- share code

def test_a_share_code_round_trips():
    code = theme.to_share_code(DARKISH)
    assert code.startswith("BT1.")
    assert "=" not in code and "+" not in code and "/" not in code
    assert theme.from_share_code(code) == DARKISH


def test_a_share_code_is_base64url_json_of_the_custom_fields():
    import base64
    body = theme.to_share_code(DARKISH)[4:]
    body += "=" * (-len(body) % 4)
    fields = json.loads(base64.urlsafe_b64decode(body))
    assert fields == {k: DARKISH[k] for k in ("c", "w", "ends", "dot", "by")}


def test_the_builder_page_and_python_agree_on_the_share_code():
    # The page writes JSON.stringify of the same five fields, in this order,
    # with no spaces; Python must decode exactly that.
    import base64
    js = json.dumps({"c": DARKISH["c"], "w": 12, "ends": "round", "dot": 1,
                     "by": "level"}, separators=(",", ":"))
    code = "BT1." + base64.urlsafe_b64encode(js.encode()).decode().rstrip("=")
    assert theme.from_share_code(code) == DARKISH
    assert theme.to_share_code(DARKISH) == code


@pytest.mark.parametrize("bad", [
    "BT2.abc", "BT1.", "BT1.!!!!", "BT1." + "A" * 900, "nonsense",
    "BT1.bm90IGpzb24",                         # "not json"
    "BT1.WzFd",                                # [1]
])
def test_a_bad_share_code_is_refused(bad):
    with pytest.raises(theme.ThemeError):
        theme.from_share_code(bad)


def test_a_share_code_with_unreadable_text_is_refused():
    t = custom(c=["777777", "999999"] + DARKISH["c"][2:])
    import base64
    js = json.dumps({k: t[k] for k in ("c", "w", "ends", "dot", "by")})
    code = "BT1." + base64.urlsafe_b64encode(js.encode()).decode()
    with pytest.raises(theme.ThemeError) as e:
        theme.from_share_code(code)
    assert "the board cannot use" in str(e.value)
    assert "3.05:1" in str(e.value)


@pytest.mark.parametrize("bad, says", [
    ("nonsense", "they start BT1."),
    ("BT2.abc", "they start BT1."),
    # Each of these starts BT1., so "they start BT1." was the wrong answer
    # (audit S2, 2026-10-28).
    ("BT1.", "damaged or cut short"),
    ("BT1.garbage", "damaged or cut short"),
    ("BT1.bm90IGpzb24", "damaged or cut short"),          # "not json"
    ("BT1.WzFd", "damaged or cut short"),                  # [1]
    ("BT1.e30", "damaged or cut short"),                   # {}
    ("BT1.eyJ4IjoxfQ", "damaged or cut short"),            # {"x":1}
    ("BT1." + "A" * 900, "far too long"),
])
def test_a_bad_share_code_says_what_is_wrong_with_it(bad, says):
    with pytest.raises(theme.ThemeError) as e:
        theme.from_share_code(bad)
    assert says in str(e.value)
    if bad.startswith("BT1."):
        assert "they start BT1." not in str(e.value)


def test_a_built_in_theme_has_no_share_code():
    assert theme.to_share_code({"id": 4}) is None


# ------------------------------------------------------------- k=v

def test_set_by_name():
    assert theme.from_args(["neon"], None) == {"id": 1}
    assert theme.from_args(["retro-lcd"], None) == {"id": 7}
    assert theme.from_args(["Light_Soft"], None) == {"id": 10}


def test_custom_is_not_a_name_you_can_set_bare():
    with pytest.raises(theme.ThemeError):
        theme.from_args(["custom"], None)


def test_set_kv_starts_from_dark_when_nothing_custom_is_saved():
    t = theme.from_args(["high=#00ff00"], {"id": 3})
    assert t["id"] == 12
    assert t["c"][6] == "00ff00"
    assert t["c"][:6] == DARKISH["c"][:6]


def test_set_kv_edits_the_saved_custom_theme():
    saved = custom(w=5)
    t = theme.from_args(["bg=101010", "width=thick", "arcs=per-gauge",
                         "ends=flat", "dot=off"], saved)
    assert t["c"][0] == "101010"
    assert (t["w"], t["by"], t["ends"], t["dot"]) == (22, "gauge", "flat", 0)


def test_set_kv_accepts_the_builders_own_spelling():
    t = theme.from_args(["gauge1=ABCDEF", "gauge2=#123456", "width=5",
                         "by=gauge", "dot=on"], None)
    assert t["c"][2:4] == ["abcdef", "123456"]
    assert (t["w"], t["by"], t["dot"]) == (5, "gauge", 1)


@pytest.mark.parametrize("args", [
    ["colour=ff0000"], ["bg"], ["bg=red"], ["width=8"], ["dot=maybe"],
    ["text=0e1116"],                           # text on its own background
    [], ["neon", "bg=000000"],
])
def test_bad_kv_is_refused(args):
    with pytest.raises(theme.ThemeError):
        theme.from_args(args, None)


def test_set_args_reads_the_command_the_builder_prints():
    t = custom(by="gauge", w=22, dot=0, ends="flat")
    assert theme.from_args(theme.as_args(t), None) == t


# ------------------------------------------------------------- files

def test_files_live_under_the_blink_home_given(tmp_path):
    home = str(tmp_path / ".blink")
    assert theme.theme_path(home) == os.path.join(home, "theme.json")
    assert theme.preview_path(home) == os.path.join(home,
                                                    "theme_preview.json")


def test_save_and_load(tmp_path):
    home = str(tmp_path / ".blink")
    assert theme.load_saved(home) is None
    theme.save(home, DARKISH)
    assert theme.load_saved(home) == DARKISH
    assert os.listdir(home) == ["theme.json"]


def test_a_broken_theme_file_reads_as_nothing(tmp_path):
    home = tmp_path / ".blink"
    home.mkdir()
    (home / "theme.json").write_text("{not json")
    assert theme.load_saved(str(home)) is None
    (home / "theme.json").write_text('{"id": 77}')
    assert theme.load_saved(str(home)) is None


def test_save_refuses_a_bad_theme(tmp_path):
    with pytest.raises(theme.ThemeError):
        theme.save(str(tmp_path / ".blink"), {"id": 50})
    assert not (tmp_path / ".blink" / "theme.json").exists()


def test_a_preview_is_taken_once(tmp_path):
    home = str(tmp_path / ".blink")
    theme.write_preview(home, {"id": 2})
    assert theme.take_preview(home) == ("preview", {"id": 2})
    assert theme.take_preview(home) is None
    assert not os.path.exists(theme.preview_path(home))


def test_a_cancel_replaces_a_preview_not_yet_taken(tmp_path):
    home = str(tmp_path / ".blink")
    theme.write_preview(home, {"id": 2})
    theme.write_cancel(home)
    assert theme.take_preview(home) == ("cancel", None)


def test_a_garbled_preview_file_is_dropped_unsent(tmp_path):
    home = tmp_path / ".blink"
    home.mkdir()
    (home / "theme_preview.json").write_text(
        '{"op":"preview","theme":{"id":12,"c":["zz"]}}')
    assert theme.take_preview(str(home)) is None
    assert not (home / "theme_preview.json").exists()


# ------------------------------------------------------------- the hash

def test_the_custom_hash_is_sha1_of_the_26_contract_bytes():
    import hashlib
    raw = bytes([12]) + bytes.fromhex("".join(DARKISH["c"])) + bytes(
        [12, 0, 1, 0])
    assert len(raw) == 26
    assert theme.theme_hash(DARKISH) == hashlib.sha1(raw).hexdigest()[:8]


def test_the_hash_moves_with_every_custom_field():
    seen = {theme.theme_hash(DARKISH)}
    for over in (dict(w=5), dict(ends="flat"), dict(dot=0), dict(by="gauge"),
                 dict(c=["0e1117"] + DARKISH["c"][1:]),
                 dict(c=DARKISH["c"][:6] + ["ff1901"])):
        seen.add(theme.theme_hash(custom(**over)))
    assert len(seen) == 7


def test_the_dark_like_example_hash_both_sides_check_against():
    # Quoted in the report to 17-FW, so firmware and daemon can be checked
    # against one number.
    assert theme.theme_hash(DARKISH) == "7c534a4e"


def test_a_built_in_has_no_hash():
    assert theme.fingerprint({"id": 4}) == {"id": 4}
    assert theme.fingerprint(DARKISH) == {"id": 12,
                                          "h": theme.theme_hash(DARKISH)}


# ------------------------------------------------------------- daemon sync

def now_msg(tid, why, t=None):
    m = {"t": "theme_now", "v": 2, "id": tid, "why": why}
    if t is not None:
        m["h"] = theme.theme_hash(t)
    return m


def saves(lines):
    return [m for m in lines if m.get("op") == "save"]


def test_nothing_is_sent_when_this_computer_never_set_a_theme(tmp_path):
    home = str(tmp_path / ".blink")
    s = theme.ThemeSync(home)
    s.board_now(now_msg(4, "boot"))
    assert s.outgoing() == []
    assert theme.load_saved(home) is None


def test_a_boot_report_is_never_adopted(tmp_path):
    # `blink theme set ocean`, then the daemon starts without resetting the
    # board, and the board answers the welcome with what it holds: dark.
    home = str(tmp_path / ".blink")
    theme.save(home, {"id": 4})
    s = theme.ThemeSync(home)
    s.board_now(now_msg(0, "boot"))
    assert theme.load_saved(home) == {"id": 4}
    assert s.outgoing() == [theme.message("save", {"id": 4})]


def test_a_reboot_does_not_flip_the_computers_theme(tmp_path):
    home = str(tmp_path / ".blink")
    theme.save(home, {"id": 6})
    s = theme.ThemeSync(home)
    for _ in range(3):
        s.hello()
        s.board_now(now_msg(1, "boot"))
        assert theme.load_saved(home) == {"id": 6}


def test_a_saved_report_confirms_and_is_not_adopted(tmp_path):
    home = str(tmp_path / ".blink")
    theme.save(home, {"id": 6})
    s = theme.ThemeSync(home)
    s.board_now(now_msg(0, "boot"))
    assert saves(s.outgoing()) == [theme.message("save", {"id": 6})]
    s.board_now(now_msg(6, "saved"))
    assert s.outgoing() == []
    assert theme.load_saved(home) == {"id": 6}
    with open(theme.sent_path(home), encoding="utf-8") as f:
        assert json.load(f) == {"id": 6}


def test_only_a_user_choice_on_the_board_is_adopted(tmp_path):
    home = str(tmp_path / ".blink")
    theme.save(home, {"id": 6})
    s = theme.ThemeSync(home)
    s.board_now(now_msg(6, "boot"))
    s.board_now(now_msg(1, "user"))
    assert theme.load_saved(home) == {"id": 1}
    assert s.outgoing() == []
    # And the next boot does not put Forest back.
    s.hello()
    s.board_now(now_msg(1, "boot"))
    assert s.outgoing() == []


def test_an_unknown_why_is_read_as_a_report_not_a_choice(tmp_path):
    home = str(tmp_path / ".blink")
    theme.save(home, {"id": 6})
    s = theme.ThemeSync(home)
    s.board_now({"t": "theme_now", "v": 2, "id": 1})
    s.board_now({"t": "theme_now", "v": 2, "id": 2, "why": "sparkle"})
    assert theme.load_saved(home) == {"id": 6}


def test_the_board_choosing_custom_keeps_the_saved_colours(tmp_path):
    home = str(tmp_path / ".blink")
    theme.save(home, DARKISH)
    s = theme.ThemeSync(home)
    s.board_now(now_msg(12, "boot", DARKISH))
    s.board_now(now_msg(3, "user"))
    assert theme.load_saved(home) == {"id": 3}
    # Settings > Theme > Custom puts back the colours the board still has.
    s.board_now(now_msg(12, "user", DARKISH))
    assert theme.load_saved(home) == DARKISH
    assert s.outgoing() == []


def test_the_board_choosing_custom_we_never_saw_forgets_ours(tmp_path):
    home = str(tmp_path / ".blink")
    theme.save(home, DARKISH)
    s = theme.ThemeSync(home)
    other = custom(w=22)
    s.board_now(now_msg(12, "user", other))
    assert theme.load_saved(home) is None
    assert s.outgoing() == []


def test_custom_to_custom_gets_through(tmp_path):
    home = str(tmp_path / ".blink")
    a, b = DARKISH, custom(c=DARKISH["c"][:6] + ["ff00aa"])
    theme.save(home, b)
    s = theme.ThemeSync(home)
    s.board_now(now_msg(12, "boot", a))
    assert saves(s.outgoing()) == [theme.message("save", b)]


def test_the_same_custom_theme_is_not_sent_again(tmp_path):
    home = str(tmp_path / ".blink")
    theme.save(home, DARKISH)
    s = theme.ThemeSync(home)
    s.board_now(now_msg(12, "boot", DARKISH))
    assert s.outgoing() == []


def test_a_custom_report_with_no_hash_is_a_difference(tmp_path):
    home = str(tmp_path / ".blink")
    theme.save(home, DARKISH)
    s = theme.ThemeSync(home)
    s.board_now({"t": "theme_now", "v": 2, "id": 12, "why": "boot",
                 "h": "not-hex!"})
    assert saves(s.outgoing()) == [theme.message("save", DARKISH)]


def test_one_save_per_difference_never_a_duplicate(tmp_path):
    home = str(tmp_path / ".blink")
    theme.save(home, {"id": 6})
    s = theme.ThemeSync(home)
    s.board_now(now_msg(0, "boot"))
    assert len(saves(s.outgoing())) == 1
    # The board has not confirmed yet; the next ticks say nothing.
    for _ in range(5):
        assert s.outgoing() == []
    # A new difference: the owner sets another theme here.
    theme.save(home, {"id": 7})
    assert saves(s.outgoing()) == [theme.message("save", {"id": 7})]
    assert s.outgoing() == []


def test_a_save_lost_to_a_reboot_is_sent_again(tmp_path):
    home = str(tmp_path / ".blink")
    theme.save(home, {"id": 6})
    s = theme.ThemeSync(home)
    s.board_now(now_msg(0, "boot"))
    assert len(saves(s.outgoing())) == 1
    s.hello()
    s.board_now(now_msg(0, "boot"))
    assert saves(s.outgoing()) == [theme.message("save", {"id": 6})]


def test_a_reboot_alone_makes_an_unconfirmed_save_owed_again(tmp_path):
    # The board rebooted (hello) and its boot report has not arrived -- or
    # was lost. The save sent before the reboot was never confirmed, so it
    # may be gone with the reboot: hello() must forget it was asked.
    home = str(tmp_path / ".blink")
    theme.save(home, {"id": 6})
    s = theme.ThemeSync(home)
    assert len(saves(s.outgoing())) == 1
    assert s.outgoing() == []
    s.hello()
    assert saves(s.outgoing()) == [theme.message("save", {"id": 6})]


def test_with_no_report_the_last_confirmed_theme_is_the_reference(tmp_path):
    # theme_sent.json says the board confirmed Forest; theme.json is Forest;
    # nothing has been heard yet. No save: the board was proved to hold it.
    home = str(tmp_path / ".blink")
    theme.save(home, {"id": 6})
    s = theme.ThemeSync(home)
    s.board_now(now_msg(6, "saved"))
    s2 = theme.ThemeSync(home)
    assert s2.outgoing() == []


def test_with_no_report_and_no_confirmation_theme_json_is_sent(tmp_path):
    # The old code assumed the board already held theme.json, so a theme
    # set while the daemon was down never reached the board.
    home = str(tmp_path / ".blink")
    theme.save(home, {"id": 6})
    s = theme.ThemeSync(home)
    assert s.outgoing() == [theme.message("save", {"id": 6})]


def test_a_confirmed_custom_theme_is_remembered_by_hash(tmp_path):
    home = str(tmp_path / ".blink")
    theme.save(home, DARKISH)
    s = theme.ThemeSync(home)
    s.board_now(now_msg(12, "saved", DARKISH))
    with open(theme.sent_path(home), encoding="utf-8") as f:
        assert json.load(f) == theme.fingerprint(DARKISH)
    theme.save(home, custom(dot=0))
    assert saves(theme.ThemeSync(home).outgoing()) == [
        theme.message("save", custom(dot=0))]


def test_a_bad_theme_now_is_ignored(tmp_path):
    home = str(tmp_path / ".blink")
    theme.save(home, {"id": 6})
    s = theme.ThemeSync(home)
    for bad in (None, "6", 99, -1, True):
        s.board_now({"t": "theme_now", "v": 2, "id": bad, "why": "user"})
    assert theme.load_saved(home) == {"id": 6}


def test_a_preview_goes_out_once_and_before_a_save(tmp_path):
    home = str(tmp_path / ".blink")
    s = theme.ThemeSync(home)
    theme.write_preview(home, {"id": 9})
    theme.save(home, {"id": 9})
    out = s.outgoing()
    assert out == [theme.message("preview", {"id": 9}),
                   theme.message("save", {"id": 9})]
    assert s.outgoing() == []


def test_a_cancel_goes_out(tmp_path):
    home = str(tmp_path / ".blink")
    s = theme.ThemeSync(home)
    theme.write_cancel(home)
    assert s.outgoing() == [theme.message("cancel")]


def test_a_cancel_is_dropped_when_a_save_goes_out_with_it(tmp_path):
    # Review NICE-4: Save leaves a cancel. Sent before the save, it would
    # repaint the old stored theme for a moment; the save ends the preview.
    home = str(tmp_path / ".blink")
    s = theme.ThemeSync(home)
    s.board_now(now_msg(4, "boot"))
    theme.save(home, {"id": 6})
    theme.write_cancel(home)
    assert s.outgoing() == [theme.message("save", {"id": 6})]
    assert s.outgoing() == []


def test_a_cancel_goes_out_when_the_save_was_already_sent(tmp_path):
    home = str(tmp_path / ".blink")
    s = theme.ThemeSync(home)
    s.board_now(now_msg(4, "boot"))
    theme.save(home, {"id": 6})
    assert s.outgoing() == [theme.message("save", {"id": 6})]
    theme.write_cancel(home)
    assert s.outgoing() == [theme.message("cancel")]


def test_a_cancel_is_taken_and_not_sent_to_a_board_without_themes(tmp_path):
    home = str(tmp_path / ".blink")
    s = theme.ThemeSync(home)
    theme.write_cancel(home)
    assert s.outgoing(deliver=False) == []
    assert theme.take_preview(home) is None


def test_an_old_board_is_sent_nothing_and_the_preview_is_dropped(tmp_path):
    home = str(tmp_path / ".blink")
    s = theme.ThemeSync(home)
    theme.write_preview(home, {"id": 9})
    theme.save(home, {"id": 9})
    assert s.outgoing(deliver=False) == []
    assert theme.take_preview(home) is None
    assert s.outgoing() == [theme.message("save", {"id": 9})]


def test_a_windows_handle_on_theme_json_is_retried(tmp_path, monkeypatch):
    # An antivirus scan or an editor holding the file for a moment is
    # WinError 32, not a failed save.
    home = str(tmp_path / ".blink")
    real = os.replace
    calls = []

    def flaky(src, dst):
        calls.append(dst)
        if len(calls) == 1:
            e = PermissionError(13, "in use")
            e.winerror = 32
            raise e
        return real(src, dst)
    from pc import update
    monkeypatch.setattr(os, "replace", flaky)
    monkeypatch.setattr(update, "REPLACE_DELAY", 0)
    theme.save(home, {"id": 2})
    assert theme.load_saved(home) == {"id": 2}
    assert len(calls) == 2


def test_a_refused_write_is_reported_not_swallowed(tmp_path, monkeypatch):
    home = str(tmp_path / ".blink")

    def refuse(src, dst):
        raise PermissionError(13, "denied")
    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(OSError):
        theme.save(home, {"id": 2})
    assert os.listdir(home) == []


def test_the_contrast_floor_sits_above_the_boards():
    # The board refuses below 3:1 after its own rounding; a theme the PC
    # passes at 3.01 must not be one the board turns away.
    assert theme.MIN_CONTRAST == 3.05


def test_the_sync_resolves_nothing_at_import():
    # The home is handed in; the module holds no path of its own.
    src = open(theme.__file__, encoding="utf-8").read()
    assert "expanduser" not in src


# ------------------------------------------------------------- the temp file

def test_a_folder_that_refuses_new_files_fails_at_once(tmp_path, monkeypatch):
    # tempfile.mkstemp on Python 3.11 reads PermissionError as "name taken"
    # and tries the next of os.TMP_MAX names -- about two billion on Windows
    # -- on the daemon's serial thread (review S4, 2026-10-28). The write
    # must give up on the first refusal, and say so.
    home = str(tmp_path / ".blink")
    os.makedirs(home)
    real_open = os.open
    tries = []

    def refuse(path, flags, *a, **k):
        if flags & os.O_EXCL:
            tries.append(path)
            raise PermissionError(13, "refused by policy")
        return real_open(path, flags, *a, **k)
    monkeypatch.setattr(os, "open", refuse)
    with pytest.raises(PermissionError):
        theme.save(home, {"id": 2})
    assert len(tries) == 1
    assert os.listdir(home) == []


def test_the_module_does_not_use_mkstemp():
    src = open(theme.__file__, encoding="utf-8").read()
    assert "import tempfile" not in src
    assert "mkstemp(" not in src


def test_a_killed_writes_temp_file_is_one_uninstall_finds(tmp_path,
                                                          monkeypatch):
    # A write killed between its temp file and the replace leaves the temp
    # behind. It must carry a name install_statusline.temp_siblings finds,
    # which the sweep and `blink uninstall` both go by.
    from pc import install_statusline, update
    home = str(tmp_path / ".blink")
    left = []

    def die(src, dst, **k):
        left.append(src)
        raise KeyboardInterrupt
    monkeypatch.setattr(update, "_replace", die)
    monkeypatch.setattr(install_statusline, "_remove", lambda p: None)
    with pytest.raises(KeyboardInterrupt):
        theme.save(home, {"id": 2})
    assert install_statusline.temp_siblings(theme.theme_path(home)) == left


def test_a_stale_temp_file_is_swept_on_the_next_write(tmp_path):
    from pc import install_statusline
    home = str(tmp_path / ".blink")
    os.makedirs(home)
    stale = theme.theme_path(home) + ".0123456789abcdef.blink-tmp"
    with open(stale, "w") as f:
        f.write("{}")
    old = os.path.getmtime(stale) - install_statusline.STALE_TEMP_S - 60
    os.utime(stale, (old, old))
    theme.save(home, {"id": 3})
    assert os.listdir(home) == ["theme.json"]


def test_the_refusal_names_the_floor_it_enforces_and_does_not_round_up():
    """Review of 1.4.0 (builder), N2. The floor is 3.05 (MIN_CONTRAST) but
    the words said 3:1, so 949494 on white was refused as "3.03:1, and the
    board needs at least 3:1": over the floor named, under the one enforced.
    8787fb on white measures 3.0484, which rounds to 3.05 and was refused
    as "3.05:1" against a floor of 3.05. The measured ratio is cut, not
    rounded, so it never reaches the floor it was refused for."""
    t = custom(c=["ffffff", "8787fb"] + DARKISH["c"][2:])
    with pytest.raises(theme.ThemeError) as e:
        theme.validate(t)
    assert str(e.value) == ("text on background is 3.04:1, and the board"
                            " needs at least 3.05:1 to be readable")
    t = custom(c=["ffffff", "949494"] + DARKISH["c"][2:])
    with pytest.raises(theme.ThemeError) as e:
        theme.validate(t)
    assert "3.03:1" in str(e.value) and "at least 3.05:1" in str(e.value)
