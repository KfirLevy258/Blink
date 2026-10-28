"""Every string the firmware can put on the panel is drawable, and reads as
plain text there.

The Edit slot said "Run `blink theme` on your computer", and the panel drew
the backticks (audit of develop/fork-picks, 2026-10-28): Markdown habit
from docs and terminals, on a screen that renders no Markdown. A backtick
is a glyph Montserrat has, so nothing failed; the copy just looked broken.

The fonts the board links are LVGL's built-in Montserrat 14/16/20, which
carry printable ASCII, the degree sign, the bullet (U+2022) and the LVGL
symbols (Font Awesome, U+F000-F8FF), plus font_focus_40 for the Focus
figure. Anything else draws as an empty box -- which is why fmt_ascii()
exists for text that arrives from the computer. Text the firmware writes
itself has no such net, so it is checked here instead.

Every C string literal under firmware/src is checked, not only those passed
to a label: telling the two apart reliably means parsing C, and a wire
format or a log line never needs a backtick or a curly quote either.
"""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "firmware" / "src"

# Beyond printable ASCII, what LV_FONT_MONTSERRAT_* carry (lv_font_montserrat
# _14.c's cmaps: 0x20-0x7F, 0xB0, 0x2022, and the symbol list).
EXTRA = {0xB0, 0x2022}
SYMBOLS = range(0xF000, 0xF900)
# Characters the font has but the panel must not show: Markdown's code
# marks, which read as stray punctuation on a screen that renders none.
REFUSED = {"`"}


def _bt():
    spec = importlib.util.spec_from_file_location(
        "board_touch", ROOT / "tools" / "board_touch.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _bad_chars(raw):
    text = raw.decode("utf-8", "replace")
    out = []
    for ch in text:
        cp = ord(ch)
        if ch in REFUSED:
            out.append(ch)
        elif ch in "\r\n\t" or 0x20 <= cp < 0x7F:
            continue
        elif cp in EXTRA or cp in SYMBOLS:
            continue
        else:
            out.append(ch)
    return out


def test_no_firmware_string_holds_a_character_the_panel_draws_badly():
    bt = _bt()
    found = []
    for path in sorted(SRC.glob("*.[ch]")):
        for raw in bt.c_literals(path.read_text(encoding="utf-8")):
            bad = _bad_chars(raw)
            if bad:
                found.append((path.name, raw[:60], bad))
    assert not found, found


def test_the_check_would_have_caught_the_backticks():
    assert _bad_chars(b"Run `blink theme` on your computer") == ["`", "`"]
    assert _bad_chars("Reading is old — wait".encode()) == ["—"]
    assert _bad_chars("Session 42% • 3h".encode()) == []
    assert _bad_chars(b"\xef\x80\x8c ok") == []      # LV_SYMBOL_OK
