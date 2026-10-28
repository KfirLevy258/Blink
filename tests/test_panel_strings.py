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
import re
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


# One pass over the source, every token kind in the order C reads them, so a
# "//" inside a string is part of the string and a '"' is a character. The
# first version borrowed board_touch.c_literals, which strips comments
# before it looks for strings and so lost "https://" in ota_parse.c whole,
# along with anything a URL on the panel might hold (review of
# fp/audit-fixes, NICE 5).
_TOKEN = re.compile(r"""
      (?P<line>//[^\n]*)
    | (?P<block>/\*.*?\*/)
    | (?P<char>'(?:[^'\\\n]|\\.)*')
    | "(?P<str>(?:[^"\\\n]|\\.)*)"
    | (?P<sym>LV_SYMBOL_\w+)
    | (?P<other>\S)
""", re.S | re.X)


def literals(text, bt):
    """Every string literal in C source `text`, adjacent ones (and the LVGL
    symbols between them) joined, as bytes."""
    out, cur = [], None
    for m in _TOKEN.finditer(text):
        if m.group("str") is not None or m.group("sym") is not None:
            piece = (bt._c_unescape(m.group("str"))
                     if m.group("str") is not None
                     else bt._LV_SYMBOLS.get(m.group("sym"),
                                             b"\xef\x80\x80"))
            cur = piece if cur is None else cur + piece
        elif m.group("line") or m.group("block"):
            continue        # a comment between two pieces still joins them
        elif cur is not None:
            out.append(cur)
            cur = None
    if cur is not None:
        out.append(cur)
    return out


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
        for raw in literals(path.read_text(encoding="utf-8"), bt):
            bad = _bad_chars(raw)
            if bad:
                found.append((path.name, raw[:60], bad))
    assert not found, found


def test_the_check_would_have_caught_the_backticks():
    assert _bad_chars(b"Run `blink theme` on your computer") == ["`", "`"]
    assert _bad_chars("Reading is old — wait".encode()) == ["—"]
    assert _bad_chars("Session 42% • 3h".encode()) == []
    assert _bad_chars(b"\xef\x80\x8c ok") == []      # LV_SYMBOL_OK


def test_the_scan_reads_strings_the_way_c_does():
    bt = _bt()
    src = ('char q = \'"\'; /* "not a string" */ // "nor this"\n'
           'f("https://x.test/" "y", \'\\\'\'); g("a" LV_SYMBOL_OK "b");\n'
           'h("one // two");\n')
    assert literals(src, bt) == [b"https://x.test/y", b"a\xef\x80\x8cb",
                                 b"one // two"]


def test_the_url_board_touch_used_to_lose_is_scanned():
    lits = literals((SRC / "ota_parse.c").read_text(encoding="utf-8"), _bt())
    assert any(l.startswith(b"https://") for l in lits)
