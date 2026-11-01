"""The builder's board preview draws the five-hour burn view the way the
firmware does (docs/plans/burn-5h.md; firmware/src/theme.c theme_burn_inks(),
firmware/src/usage_view.c burn_draw_cb(), firmware/src/burn5h.c).

The burn view is the session gauge's half of the screen on an account with
no five-hour window, and it follows every theme. Four of its six colours are
a kind of mark no palette role describes, so theme.c derives them: Dark
starts from the contract's bytes and every other theme gets the same
relationships from its own palette through theme_mix(), and then both bar
inks are walked up to a 3:1 floor on the ground with the newest hour kept
1.5:1 above the rest. The preview has to land on the same bytes, and the page
types no palette of its own, so these tests read the weights and Dark's bytes
out of theme.c, work the expected colours out from each theme's palette in
theme.c with a port of the walk that is itself checked against the compiled
C, and compare them with the fills the page drew. A change to
theme_burn_inks() with no change to the page fails here.

Run as a browser runs it: the page's own script in node against
tests/support/builder_dom.js. The numbers and words the page ports from
burn5h.c are run beside the C itself, as the status dot is in
test_theme_builtins.py.
"""
import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from pc import theme_builtins
from tests.support import builder_page

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "firmware" / "src"
PAGE = ROOT / "pc" / "theme_builder.html"

SAVED_DARK = {"/api/state": {"status": 200,
                             "body": {"saved": {"id": 0}, "draft": None}}}

# Everything the page drew, flattened to what a test can compare: the SVG's
# children by class, fill, stroke, position and words. A node with no words
# reports none, so a shape never reads as an empty string.
_PRELUDE = """
const drawn = () => $('scr').children.map(n => ({
  cls: n.className, fill: n.attrs.fill || null, stroke: n.attrs.stroke || null,
  x: n.attrs.x === undefined ? null : +n.attrs.x,
  y: n.attrs.y === undefined ? null : +n.attrs.y,
  w: n.attrs.width === undefined ? null : +n.attrs.width,
  h: n.attrs.height === undefined ? null : +n.attrs.height,
  fs: n.attrs['font-size'] === undefined ? null : +n.attrs['font-size'],
  text: n.textContent === '' ? null : n.textContent }));
const left = () => document.querySelectorAll('[data-l]');
const pressed = () => Object.fromEntries(left().map(b => [b.dataset.l, b.getAttribute('aria-pressed')]));
await advance(10);
"""


def _run(body):
    return builder_page.run(_PRELUDE + body, SAVED_DARK)


def _words(nodes):
    return [n["text"] for n in nodes if n["text"] is not None]


# --------------------------------------------------------- the control

def test_the_session_gauge_is_the_default():
    r = _run("return { nodes: drawn(), pressed: pressed() };")
    assert r["pressed"] == {"gauge": "true", "burn": "false"}
    words = _words(r["nodes"])
    assert "SESSION 5h" in words and "WEEKLY 7d" in words and "42%" in words
    assert "BURN 5h" not in words
    assert not [n for n in r["nodes"] if n["cls"].startswith("burn-")]


def test_the_control_is_labelled_in_sentence_case():
    r = _run("return left().map(b => b.textContent);")
    assert r == ["Session gauge", "Burn view"]


def test_the_control_switches_the_left_half_and_back():
    r = _run("""
    const gauge = drawn();
    left()[1].click();
    const burn = { nodes: drawn(), pressed: pressed() };
    left()[0].click();
    return { gauge, burn, back: { nodes: drawn(), pressed: pressed() } };
    """)
    burn, back = r["burn"], r["back"]
    assert burn["pressed"] == {"gauge": "false", "burn": "true"}
    words = _words(burn["nodes"])
    # The left half is the burn view and nothing of the session gauge; the
    # weekly half, the header and the pill are what they were.
    assert "BURN 5h" in words and "tokens • 5h" in words
    assert "SESSION 5h" not in words and "3h 40m" not in words
    assert "WEEKLY 7d" in words and "1d 1h" in words and "Claude" in words
    assert "42%" not in words and "91%" in words
    assert back["pressed"] == {"gauge": "true", "burn": "false"}
    assert back["nodes"] == r["gauge"]


def test_choosing_either_half_leaves_focus_mode_for_the_gauges():
    # Focus keeps today's screen on the board, burn view or not, so the
    # choice has nothing to show there: it brings back the screen it is for.
    r = _run("""
    document.querySelectorAll('[data-v]')[1].click();   // Focus mode
    const inFocus = drawn().map(n => n.text);
    left()[1].click();
    return { inFocus, after: drawn().map(n => n.text),
             v: document.querySelectorAll('[data-v]').map(b => b.getAttribute('aria-pressed')) };
    """)
    assert "BURN 5h" not in r["inFocus"]
    assert "BURN 5h" in r["after"]
    assert r["v"] == ["true", "false"]


def test_the_burn_view_is_not_part_of_the_theme_and_sends_nothing():
    r = _run("""
    const before = requests.length;
    left()[1].click();
    await advance(500);
    return requests.length - before;
    """)
    assert r == 0


def test_with_no_session_reading_only_the_weekly_can_be_almost_used_up():
    r = _run("""
    $('p1').value = 99; $('p1').fire('input');
    const gauge = drawn().map(n => n.text);
    left()[1].click();
    return { gauge, burn: drawn().map(n => n.text) };
    """)
    assert "Session almost used up" in r["gauge"]
    assert "Session almost used up" not in r["burn"]
    assert "14:05" in r["burn"]


# ----------------------------------------------- what it says and draws

def test_the_sample_reads_as_the_rising_preset_does_on_the_board():
    r = _run("left()[1].click(); return drawn();")
    by = {}
    for n in r:
        by.setdefault(n["cls"], []).append(n)
    # BURN_PRESETS["rising"], in exact tokens: 965,000 in all, and 483,000
    # in the last complete hour (buckets 15-18) against 131,000 in the one
    # before it (11-14). The newest bucket, 265,000, is drawn, not counted.
    assert [n["text"] for n in by["burn-total"]] == ["965k"]
    assert [n["text"] for n in by["burn-rate"]] == ["483k/h • rising"]
    assert [n["text"] for n in by["burn-unit"]] == ["tokens • 5h"]
    assert [n["text"] for n in by["burn-name"]] == ["BURN 5h"]
    # Sixteen bars, four of the twenty buckets being empty; the newest four
    # are the hot ones, all on a baseline at 148 and 5 px wide.
    assert len(by["burn-hot"]) == 4 and len(by["burn-cold"]) == 12
    bars = by["burn-cold"] + by["burn-hot"]
    assert [n["h"] for n in by["burn-hot"]] == [20, 30, 44, 56]   # v * 56 / 265
    assert by["burn-cold"][0]["h"] == 2          # 4k against 265k: the floor
    assert all(n["y"] + n["h"] == 148 and n["w"] == 5 for n in bars)
    assert [n["x"] for n in bars] == [20 + 6 * i for i in range(4, 20)]
    base = by["burn-base"][0]
    assert (base["x"], base["y"], base["w"], base["h"]) == (20, 148, 120, 1)
    # Each full local hour: a tick on its bucket's left edge, its number
    # under it. The newest bucket starts at 14:00.
    assert [n["text"] for n in by["burn-hour"]] == ["10", "11", "12", "13", "14"]
    assert [n["x"] for n in by["burn-tick"]] == [20 + 6 * i for i in (3, 7, 11, 15, 19)]
    assert [n["x"] for n in by["burn-hour"]] == [n["x"] for n in by["burn-tick"]]
    # Rising: the arrow is in g1, the number in the text colour.
    assert by["burn-glyph"][0]["stroke"] == "#4387DF"
    assert by["burn-total"][0]["fill"] == "#E6E8EB"


# ------------------------------------------------------------ colours

def _c(name):
    return (SRC / name).read_text(encoding="utf-8")


def theme_mix(a, b, t):
    """theme.c theme_mix(): per channel, round half up, clamped."""
    out = ""
    for i in (0, 2, 4):
        x, y = int(a[i:i + 2], 16), int(b[i:i + 2], 16)
        out += "%02X" % max(0, min(255, int(x + (y - x) * t + 0.5)))
    return out


def _firmware_inks():
    """What theme_burn_inks() says, read from the C: Dark's four bytes, and
    the roles and weight each of the others is mixed from."""
    src = _c("theme.c")
    start = src.index("void theme_burn_inks(")
    body = src[start:src.index("\n}\n", start)]
    dark = {k: v[2:].upper()
            for k, v in re.findall(r"k->(\w+) = (0x[0-9A-F]{6});", body)}
    mixes = {k: (a, b, float(t)) for k, a, b, t in re.findall(
        r"k->(\w+) = theme_mix\(p->(\w+), p->(\w+), ([0-9.]+)\);", body)}
    assert set(dark) == set(mixes) == {"hot", "cold", "base", "tick"}, body
    return dark, mixes


def _expected(pal, dark=False):
    """theme_burn_inks() on a palette {bg, text, g1, track, grey}, uppercase
    hex: the mix, or Dark's bytes, then the floor."""
    dark_bytes, mixes = _firmware_inks()
    if dark:
        k = dict(dark_bytes)
    else:
        k = {n: theme_mix(pal[a], pal[b], t)
             for n, (a, b, t) in mixes.items()}
    return _floor(k, pal["bg"], pal["text"], pal["g1"])


def _floor_figures():
    """THEME_BURN_INK_FLOOR and THEME_BURN_INK_STEP, read from theme.h."""
    h = _c("theme.h")
    got = dict(re.findall(r"#define THEME_BURN_INK_(FLOOR|STEP)\s+([0-9.]+)",
                          h))
    assert set(got) == {"FLOOR", "STEP"}, got
    return float(got["FLOOR"]), float(got["STEP"])


def _floor(k, bg, text, g1):
    """theme.c theme_burn_inks()'s walk, step for step: both bar inks to
    min(3:1, the text's own) on the ground as the panel shows it, the newest
    hour 1.5:1 above the older bars, and where the text cannot give it that,
    the stronger of text and g1 with the older bars stepped back towards the
    ground no further than the floor."""
    floor_cr, step = _floor_figures()
    floor = min(floor_cr, _shown(text, bg))

    def apart(hot, cold):
        return _shown(hot, bg) > _shown(cold, bg) and _shown(hot, cold) >= step

    def walk(frm, to, cold=None):
        for pct in range(100):
            c = theme_mix(frm, to, pct / 100.0)
            if _shown(c, bg) >= floor and (cold is None or apart(c, cold)):
                return c
        return to

    k = dict(k)
    k["cold"] = walk(k["cold"], g1)
    if _shown(k["cold"], bg) < floor:
        k["cold"] = walk(g1, text)
    k["hot"] = walk(k["hot"], text, k["cold"])
    if apart(k["hot"], k["cold"]):
        return k
    k["hot"] = g1 if _shown(g1, bg) > _shown(text, bg) else text
    frm = k["cold"]
    for pct in range(1, 100):
        c = theme_mix(frm, bg, pct / 100.0)
        if _shown(c, bg) < floor:
            break
        k["cold"] = c
        if apart(k["hot"], c):
            break
    return k


def _palettes():
    """Each built-in's palette as theme.c holds it, uppercase."""
    spec = importlib.util.spec_from_file_location(
        "gen_theme_builtins", ROOT / "tools" / "gen_theme_builtins.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    pals = gen.palettes(_c("theme.c"), _c("theme.h"))
    return {b["name"]: {k: pals[b["key"].upper()][k].upper()
                        for k in ("bg", "text", "g1", "track", "grey")}
            for b in theme_builtins.BUILTINS}


def _inks_drawn(nodes):
    """The colours the page drew, by the role theme_burn_inks() names them,
    and the hour numbers' own, which are the palette's grey."""
    def fill(cls):
        got = {n["fill"] for n in nodes if n["cls"] == cls}
        assert len(got) == 1, (cls, got)
        return got.pop().lstrip("#")
    return {"hot": fill("burn-hot"), "cold": fill("burn-cold"),
            "base": fill("burn-base"), "tick": fill("burn-tick"),
            "grey": fill("burn-hour")}


def test_the_weights_the_tests_read_are_the_ones_in_the_firmware():
    # If theme_burn_inks() is rewritten so these patterns stop matching, this
    # is the test that says so, rather than the colour tests passing on nothing.
    dark, mixes = _firmware_inks()
    assert dark == {"hot": "7FB2F0", "cold": "2D5C99", "base": "3A414B",
                    "tick": "5C636B"}
    assert mixes == {"hot": ("g1", "text", 0.35), "cold": ("g1", "bg", 0.35),
                     "base": ("track", "grey", 0.30),
                     "tick": ("track", "grey", 0.77)}
    assert ("BURN_DARK = { hot: 0x7FB2F0, cold: 0x2D5C99, base: 0x3A414B,"
            " tick: 0x5C636B };") in PAGE.read_text(encoding="utf-8")


@pytest.mark.parametrize("name", [b["name"] for b in theme_builtins.BUILTINS])
def test_every_built_ins_burn_colours_are_what_theme_mix_gives(name):
    pal = _palettes()[name]
    want = _expected(pal, dark=(name == "Dark"))
    want["grey"] = pal["grey"]          # the hour numbers, on every theme
    r = _run(f"""
    preset({json.dumps(name)});
    left()[1].click();
    return drawn();
    """)
    assert _inks_drawn(r) == want, name
    by = {n["cls"]: n for n in r}
    assert by["burn-glyph"]["stroke"] == "#" + pal["g1"]
    assert by["burn-total"]["fill"] == "#" + pal["text"]


def test_dark_and_light_are_checked_against_the_firmwares_own_mix():
    # The two a failure should read plainest on, spelled out as arithmetic.
    dark, light = _palettes()["Dark"], _palettes()["Light"]
    # Dark keeps three of the contract's bytes; its older bars, #2D5C99,
    # read 2.88:1 as drawn and are walked towards g1 to the 3:1 floor.
    assert _expected(dark, dark=True) == {"hot": "7FB2F0", "cold": "2F609F",
                                          "base": "3A414B", "tick": "5C636B"}
    want = _expected(light)
    assert want["hot"] == theme_mix("2F6FCB", "111418", 0.35)
    # Light's plain mix, #78A1DD, read 2.60:1; the floor walks it back
    # towards g1.
    assert theme_mix("2F6FCB", "FFFFFF", 0.35) == "78A1DD"
    assert want["cold"] == "6796D9"
    assert want["base"] == theme_mix("DBDCDC", "898A8D", 0.30)
    assert want["tick"] == theme_mix("DBDCDC", "898A8D", 0.77)
    # On a white ground the last hour must still be the stronger mark,
    # which is what mixing towards the text (not "brighter") is for.
    assert _lum(want["hot"]) < _lum(want["cold"])
    for name, pal in (("Dark", dark), ("Light", light)):
        r = _run(f"""
        preset({json.dumps(name)});
        left()[1].click();
        return drawn();
        """)
        exp = _expected(pal, dark=(name == "Dark"))
        got = _inks_drawn(r)
        assert {k: got[k] for k in exp} == exp, name


def test_the_burn_view_follows_a_theme_change_as_the_gauges_do():
    r = _run("""
    left()[1].click();
    const seen = [];
    for (const n of ['Dark', 'Light', 'Ocean', 'Dark']) {
      preset(n);
      seen.push(drawn().filter(x => x.cls.startsWith('burn-')).map(x => x.fill));
    }
    return seen;
    """)
    dark1, light, ocean, dark2 = r
    assert dark1 == dark2
    assert len({json.dumps(x) for x in (dark1, light, ocean)}) == 3


def _q565(h):
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    r, g, b = ((r >> 3 << 3) | (r >> 5), (g >> 2 << 2) | (g >> 6),
               (b >> 3 << 3) | (b >> 5))
    return "%02X%02X%02X" % (r, g, b)


def _lum(h):
    def lin(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(int(h[i:i + 2], 16)) for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _shown(a, b):
    la, lb = _lum(_q565(a)), _lum(_q565(b))
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def _custom_grey(tx, bg):
    """theme.c custom_grey(): readable_mix(tx, bg, bg, 45, 3.0)."""
    want = min(3.0, _shown(tx, bg))
    for pct in range(45, 0, -1):
        d = theme_mix(tx, bg, pct / 100.0)
        if _shown(d, bg) >= want:
            return d
    return tx


def _custom_expected(bg, text, g1):
    """A custom theme as theme_derive() makes it: the track is 15% of the
    text into the ground, the grey is custom_grey()."""
    pal = {"bg": bg, "text": text, "g1": g1,
           "track": theme_mix(bg, text, 0.15), "grey": _custom_grey(text, bg)}
    want = _expected(pal)
    want["grey"] = pal["grey"]
    return want


# A dark ground, a light one, and a mid grey one, where the grey has to walk
# back from 45% to keep its 3:1. Then the floor's own cases: a g1 that reads
# stronger than the text, which takes the walk's second stage (one of the
# themes tests/theme found short of the step before it existed), and text
# too faint, at about 3.6:1, to leave room for the step at all.
_CUSTOMS = [("101820", "F0E6D8", "E8A33D"), ("F4EFE6", "1A2B3C", "C0392B"),
            ("808080", "FFFFFF", "00AA55"), ("0C1439", "E91FFC", "C2FC17"),
            ("101820", "6E6E6E", "3050A0")]


@pytest.mark.parametrize("bg,text,g1", _CUSTOMS)
def test_a_custom_themes_burn_colours_are_derived_as_the_board_derives_them(
        bg, text, g1):
    r = _run(f"""
    type('t-bg', '#{bg}'); type('t-text', '#{text}'); type('t-g1', '#{g1}');
    left()[1].click();
    return drawn();
    """)
    assert _inks_drawn(r) == _custom_expected(bg, text, g1)


def test_a_custom_themes_track_and_grey_agree_with_the_firmwares_derive(tmp_path):
    """The test's own derivation, checked against theme_derive() compiled
    from theme.c, so the colour tests above are about the board and not about
    a second port that is wrong the same way."""
    cc = shutil.which("cc")
    if cc is None:
        pytest.skip("needs a C compiler")
    body = "\n".join(
        f"\tt.c[0] = 0x{bg}; t.c[1] = 0x{tx}; t.c[2] = 0x{g1};\n"
        "\ttheme_derive(&t, &p);\n"
        '\tprintf("%06X %06X\\n", (unsigned)p.track, (unsigned)p.grey);'
        for bg, tx, g1 in _CUSTOMS)
    src = tmp_path / "derive.c"
    src.write_text('#include <stdio.h>\n#include "theme.h"\n'
                   "int main(void)\n{\n\tstruct theme_custom t = {{0}};\n"
                   f"\tstruct palette p;\n\n{body}\n\treturn 0;\n}}\n",
                   encoding="utf-8")
    exe = tmp_path / "derive"
    subprocess.run([cc, "-I", str(SRC), str(src), str(SRC / "theme.c"),
                    str(SRC / "msg_parse.c"), "-lm", "-o", str(exe)],
                   check=True)
    out = subprocess.run([str(exe)], check=True, capture_output=True,
                         text=True).stdout.splitlines()
    assert out == ["%s %s" % (theme_mix(bg, tx, 0.15), _custom_grey(tx, bg))
                   for bg, tx, g1 in _CUSTOMS]


def test_editing_a_colour_moves_the_burn_view_live():
    r = _run("""
    left()[1].click();
    const hot = () => drawn().filter(n => n.cls === 'burn-hot').map(n => n.fill);
    const before = hot();
    type('t-g1', '#FF8800');
    return { before, after: hot(), glyph: drawn().find(n => n.cls === 'burn-glyph').stroke };
    """)
    assert r["before"] != r["after"]
    assert r["glyph"] == "#FF8800"


def test_an_edited_dark_is_a_custom_theme_and_gets_the_derived_colours():
    # THEME_DARK keeps the contract's bytes because it is a built-in; the
    # same colours as a custom theme are THEME_CUSTOM and are derived.
    # Touching a control that makes the theme custom must take the bytes
    # away, as it does on the board.
    dark = _palettes()["Dark"]
    r = _run("""
    left()[1].click();
    document.querySelectorAll('[data-e]')[1].click();   // Flat ends
    return drawn();
    """)
    want = _custom_expected(dark["bg"], dark["text"], dark["g1"])
    assert _inks_drawn(r) == want
    assert want["hot"] != "7FB2F0"


# -------------------------------- the numbers and words, beside the C

def _burn_vectors():
    """(buckets in output tokens, q, UTC offset in minutes): the presets of
    pc/providers/scripted.py as the wire carries them, then the edge each
    rule has. The last complete hour is buckets 15-18, the one before it
    11-14, and bucket 19 is drawn but never compared."""
    from pc.providers import scripted
    q = 1793455200
    v = [(list(tokens), q, 0) for tokens in scripted.BURN_PRESETS.values()]
    v += [([0] * 19 + [1], q, 0),                    # only bucket 19: idle
          ([0] * 15 + [10, 0, 0, 0, 0], q + 900, 330),
          ([5] * 15 + [11, 11, 11, 11, 5], q, -480),
          ([100] * 15 + [110] * 4 + [0], q, 345),    # +10%: steady
          ([100] * 15 + [111] * 4 + [0], q, 0),      # over +10%: rising
          ([100] * 15 + [90] * 4 + [500], q, 0),     # -10%: steady
          ([100] * 15 + [89] * 4 + [0], q, 0),       # under -10%: easing
          ([0] * 15 + [3, 0, 0, 0, 0], q + 1800, 60),  # before zero: rising
          # The review of 2026-10-31's cases: 1,000 tokens a minute is
          # steady at every minute of the quarter-hour still filling, and
          # 2,001 then 1,999 a quarter-hour is steady, not "easing".
          ([15000] * 19 + [0], q, 0),
          ([15000] * 19 + [8000], q, 0),
          ([15000] * 19 + [14000], q, 0),
          ([0] * 11 + [2001] * 4 + [1999] * 4 + [0], q, 0),
          ([0] * 11 + [1000] * 4 + [4] * 4 + [0], q, 0),
          ([99999999] * 20, q, 0)]
    return v


# Tokens either side of every rounding edge the format has.
_FMT_KS = [0, 1, 7, 412, 999, 1000, 1049, 1050, 1099, 1100, 2149, 2150, 9949,
           9950, 9999, 10000, 10499, 10500, 99499, 99500, 100000, 999499,
           999500, 999999, 1000000, 1049999, 1050000, 9949999, 9950000,
           99999999, 999499999, 999500000, 1999999980]


def _js_functions(html, names):
    """The source of each named `function NAME(...) {...}`, by brace
    matching (none of these holds a brace inside a string)."""
    out = []
    for name in names:
        start = html.index(f"function {name}(")
        depth, i = 0, html.index("{", start)
        while True:
            depth += {"{": 1, "}": -1}.get(html[i], 0)
            i += 1
            if depth == 0:
                break
        out.append(html[start:i])
    return "\n".join(out)


def test_the_ported_decisions_match_burn5h_c_line_for_line(tmp_path):
    """Total, rate, direction, bar heights and the hour of each bucket, run
    through burn5h.c compiled for the host and through the page's functions
    on the same inputs. The ports are exact or this fails."""
    cc, node = shutil.which("cc"), shutil.which("node")
    if cc is None or node is None:
        pytest.skip("needs a C compiler and node")
    vectors = _burn_vectors()
    lines = [f'\tburn5h_fmt_tokens({k}u, n, sizeof n);'
             f' printf("fmt %u %s\\n", {k}u, n);' for k in _FMT_KS]
    for b, q, off in vectors:
        arr = ", ".join(f"{x}u" for x in b)
        lines.append(
            "\t{ static const uint32_t b[20] = {" + arr + "};"
            " struct burn5h m; uint32_t mx = 0;"
            " memset(&m, 0, sizeof m); memcpy(m.b, b, sizeof b);"
            f" m.q = {q}LL;"
            " for (int i = 0; i < 20; i++) if (m.b[i] > mx) mx = m.b[i];"
            " burn5h_fmt_tokens(burn5h_total(&m), n, sizeof n);"
            ' printf("view %s | ", n);'
            " burn5h_fmt_rate(&m, n, sizeof n);"
            ' printf("%s |", n);'
            ' for (int i = 0; i < 20; i++)'
            ' printf(" %d", burn5h_bar_h(m.b[i], mx, 56, 2));'
            ' printf(" |");'
            ' for (int i = 0; i < 20; i++)'
            f' printf(" %d", burn5h_hour(m.q, i, {off}));'
            ' printf("\\n"); }')
    src = tmp_path / "burn.c"
    src.write_text('#include <stdio.h>\n#include <string.h>\n'
                   '#include "burn5h.h"\n'
                   "int main(void)\n{\n\tchar n[32];\n\n"
                   + "\n".join(lines) + "\n\treturn 0;\n}\n",
                   encoding="utf-8")
    exe = tmp_path / "burn"
    subprocess.run([cc, "-I", str(SRC), str(src), str(SRC / "burn5h.c"),
                    str(SRC / "msg_parse.c"), "-o", str(exe)], check=True)
    c_out = subprocess.run([str(exe)], check=True, capture_output=True,
                           encoding="utf-8").stdout.splitlines()

    html = PAGE.read_text(encoding="utf-8")
    js = re.search(r"var BURN_N = [^;]*;", html).group(0) + "\n"
    js += _js_functions(html, ("burnSum", "burnLastHour", "burnDir",
                               "burnTokens", "burnBarH", "burnHour"))
    js += f"\nconst ks = {json.dumps(_FMT_KS)}, vs = {json.dumps(_burn_vectors())};\n"
    js += """
const out = [];
for (const k of ks) out.push('fmt ' + k + ' ' + burnTokens(k));
for (const [b, q, off] of vs) {
  const mx = Math.max(...b);
  out.push('view ' + burnTokens(burnSum(b, 0, 20)) + ' | '
    + burnTokens(burnLastHour(b)) + '/h \\u2022 ' + burnDir(b) + ' |'
    + b.map(v => ' ' + burnBarH(v, mx, 56, 2)).join('') + ' |'
    + b.map((v, i) => ' ' + burnHour(q, i, off)).join(''));
}
process.stdout.write(out.join('\\n') + '\\n');
"""
    (tmp_path / "burn.js").write_text(js, encoding="utf-8")
    js_out = subprocess.run([node, str(tmp_path / "burn.js")], check=True,
                            capture_output=True,
                            encoding="utf-8").stdout.splitlines()
    assert len(c_out) == len(_FMT_KS) + len(vectors)
    assert js_out == c_out


# ------------------------------------------------- the floor, as drawn

def _check_floor(got, bg, text):
    """Both bars at min(3:1, text) on the ground, as the panel shows them;
    and, where the text reads 5:1 or more, the newest hour the stronger by
    1.5:1 (tests/theme measured 4.8 as where the step always holds)."""
    floor_cr, step = _floor_figures()
    floor = min(floor_cr, _shown(text, bg))
    assert _shown(got["hot"], bg) >= floor, got
    assert _shown(got["cold"], bg) >= floor, got
    if _shown(text, bg) >= 5.0:
        assert _shown(got["hot"], bg) > _shown(got["cold"], bg), got
        assert _shown(got["hot"], got["cold"]) >= step, got


@pytest.mark.parametrize("name", [b["name"] for b in theme_builtins.BUILTINS])
def test_every_built_ins_burn_bars_meet_the_floor_as_drawn(name):
    # The /code-review max of 2026-11-01 measured the older bars at 2.14:1
    # (Retro LCD) to 2.88 (Dark) on five built-ins with the plain mix.
    pal = _palettes()[name]
    r = _run(f"""
    preset({json.dumps(name)});
    left()[1].click();
    return drawn();
    """)
    got = _inks_drawn(r)
    _check_floor(got, pal["bg"], pal["text"])
    # Every built-in has room for the step, whatever its text.
    floor_cr, step = _floor_figures()
    assert _shown(got["hot"], got["cold"]) >= step, (name, got)


@pytest.mark.parametrize("bg,text,g1", _CUSTOMS)
def test_a_custom_themes_burn_bars_meet_the_floor_as_drawn(bg, text, g1):
    r = _run(f"""
    type('t-bg', '#{bg}'); type('t-text', '#{text}'); type('t-g1', '#{g1}');
    left()[1].click();
    return drawn();
    """)
    _check_floor(_inks_drawn(r), bg, text)


def test_the_floor_moves_only_the_themes_it_has_to():
    # Five built-ins were under it; the other seven draw as before.
    _, mixes = _firmware_inks()
    moved = set()
    for name, pal in _palettes().items():
        if name == "Dark":
            plain = dict(_firmware_inks()[0])
        else:
            plain = {k: theme_mix(pal[a], pal[b], t)
                     for k, (a, b, t) in mixes.items()}
        if _expected(pal, dark=(name == "Dark")) != plain:
            moved.add(name)
    assert moved == {"Dark", "Retro LCD", "Colour-blind", "Light",
                     "Light soft"}


def test_the_ported_floor_is_the_firmwares_own(tmp_path):
    """theme_burn_inks() compiled from theme.c, on every built-in and on
    each custom theme above, against this file's port of it -- the oracle
    every colour test here uses -- so a walk changed on one side only fails
    here, not as a page that is wrong the same way the port is."""
    cc = shutil.which("cc")
    if cc is None:
        pytest.skip("needs a C compiler")
    body = ["\tfor (int id = 0; id < THEME_BUILTIN_N; id++) {",
            "\t\ttheme_burn_inks(theme_builtin(id), id == THEME_DARK, &k);",
            '\t\tprintf("%06X %06X %06X %06X\\n", (unsigned)k.hot,'
            " (unsigned)k.cold, (unsigned)k.base, (unsigned)k.tick);",
            "\t}"]
    for bg, tx, g1 in _CUSTOMS:
        body += [f"\tt.c[0] = 0x{bg}; t.c[1] = 0x{tx}; t.c[2] = 0x{g1};",
                 "\ttheme_derive(&t, &p);",
                 "\ttheme_burn_inks(&p, false, &k);",
                 '\tprintf("%06X %06X %06X %06X\\n", (unsigned)k.hot,'
                 " (unsigned)k.cold, (unsigned)k.base, (unsigned)k.tick);"]
    src = tmp_path / "inks.c"
    src.write_text('#include <stdio.h>\n#include "theme.h"\n'
                   "int main(void)\n{\n\tstruct theme_custom t = {{0}};\n"
                   "\tstruct palette p;\n\tstruct theme_burn_inks k;\n\n"
                   + "\n".join(body) + "\n\treturn 0;\n}\n",
                   encoding="utf-8")
    exe = tmp_path / "inks"
    subprocess.run([cc, "-I", str(SRC), str(src), str(SRC / "theme.c"),
                    str(SRC / "msg_parse.c"), "-lm", "-o", str(exe)],
                   check=True)
    out = subprocess.run([str(exe)], check=True, capture_output=True,
                         text=True).stdout.splitlines()
    pals = _palettes()
    order = [b["name"] for b in sorted(theme_builtins.BUILTINS,
                                       key=lambda b: b["id"])]
    want = []
    for name in order:
        k = _expected(pals[name], dark=(name == "Dark"))
        want.append(" ".join(k[n] for n in ("hot", "cold", "base", "tick")))
    for bg, tx, g1 in _CUSTOMS:
        k = _custom_expected(bg, tx, g1)
        want.append(" ".join(k[n] for n in ("hot", "cold", "base", "tick")))
    assert out == want


# --------------------------------------------- the rows beside the view

def _layout(name):
    m = re.search(rf"#define {name}\s+(\d+)", _c("usage_layout.h"))
    assert m, name
    return int(m.group(1))


def test_the_burn_views_caption_rows_are_the_weekly_gauges_rows():
    """On the board "BURN 5h" sits where "SESSION 5h" does and the rate line
    where the countdown does: default-font labels, Montserrat 14 in 16 px
    rows from GAUGE_NAME_Y and GAUGE_CD_Y. The page drew the burn rows that
    way and the weekly gauge's at 12 px, 1 and 3 px higher, so the two
    halves of one row did not match (the /code-review max of 2026-11-01).
    Both follow the board now."""
    r = _run("""
    const gauge = drawn();
    left()[1].click();
    return { gauge, burn: drawn() };
    """)
    name_y, cd_y = _layout("GAUGE_NAME_Y") + 13, _layout("GAUGE_CD_Y") + 13

    def rows(nodes, cls):
        return [(n["text"], n["y"], n["fs"]) for n in nodes
                if n["cls"] == cls]
    assert rows(r["gauge"], "gauge-name") == [("SESSION 5h", name_y, 14),
                                              ("WEEKLY 7d", name_y, 14)]
    assert rows(r["gauge"], "gauge-cd") == [("3h 40m", cd_y, 14),
                                            ("1d 1h", cd_y, 14)]
    assert rows(r["burn"], "gauge-name") == [("WEEKLY 7d", name_y, 14)]
    assert rows(r["burn"], "burn-name") == [("BURN 5h", name_y, 14)]
    assert [(y, fs) for _, y, fs in rows(r["burn"], "burn-rate")] == \
        [(cd_y, 14)]
