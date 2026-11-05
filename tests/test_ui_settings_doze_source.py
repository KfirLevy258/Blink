"""Nothing on lv_layer_top takes a tap meant for the eye clip or the doze.

ask_prompt_step() and upd_prompt_step() are host-tested as truth tables
(tests/ask_prompt, tests/upd_prompt), but what they are told is assembled in
ui_settings.c's 250 ms timer, which only a finger reaches. The /code-review
max of feat/usage-refresh found two inputs missing there: ask_tick's `busy`
had no eye clip (ui_anim_showing), so an ask arriving mid-clip was drawn
over it, clickable (V9); and the update offer was neither held off nor
taken down by the doze, so a waking tap could land on "Update now" (V22).
A refactor that drops either input still passes both truth tables and the
build; this reads the source, as tests/test_upd_confirm_source.py does.
"""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "firmware" / "src"


def _body(name, kind="static void"):
    src = (SRC / "ui_settings.c").read_text(encoding="utf-8")
    m = re.search(r"\n" + kind + " " + name + r"\([^)]*\)\n\{(.*?)\n\}\n",
                  src, re.S)
    assert m, name
    return m.group(1)


def _call(body, fn):
    start = body.index(fn + "(")
    depth, i = 0, start + len(fn)
    while True:
        c = body[i]
        depth += c == "("
        depth -= c == ")"
        if depth == 0:
            return body[start:i + 1]
        i += 1


def test_the_ask_waits_out_the_eye_clip():
    assert "ui_anim_showing()" in _call(_body("ask_tick"), "ask_prompt_step")


def test_the_offer_waits_out_the_eye_clip_and_the_doze():
    call = _call(_body("upd_timer_cb"), "upd_prompt_step")
    assert "ui_anim_showing()" in call
    assert call.rstrip(")").rstrip().endswith("board_dozing")


def test_the_doze_takes_both_boxes_down_at_once():
    body = _body("ui_settings_dozing", kind="void")
    assert "ask_close();" in body
    offer = body.index("upd_prompt_close();")
    # Unanswered: the latch lifts, so the offer is made again once awake.
    assert "upd_prompt_done = false;" in body[offer:]


def test_the_sleep_screen_says_so_both_ways():
    src = (SRC / "ui_sleep.c").read_text(encoding="utf-8")
    assert "ui_settings_dozing(true);" in src
    assert "ui_settings_dozing(false);" in src
    assert src.index("ui_settings_dozing(true);") < src.index(
        "ui_settings_dozing(false);")
