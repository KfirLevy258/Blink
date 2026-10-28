"""The built-in themes have one source, the firmware's table, and every
place the computer shows one agrees with it.

Before this file the builder typed its own copy of the twelve presets, and
the second round of firmware palettes changed the board's colours without
it: the audit of develop/fork-picks (2026-10-28) found Colour-blind safe,
Light, Light soft and Retro LCD previewing colours the board never drew,
and a custom theme started from one of them starting from those colours
too. Nothing failed, because nothing compared the two. The ids had the same
gap: firmware/src/theme.h and pc/theme.py NAMES agreed by care alone.

So: pc/theme_builtins.py is generated from theme.c (tools/
gen_theme_builtins.py), the builder is served that list, and the tests here
read the C sources themselves -- not the generated module -- so a stale
module, a hand edit, or a reordered enum each fail on their own.
"""
import importlib.util
import json
import math
import re
from pathlib import Path

from pc import cli
from pc import theme
from pc import theme_builtins
from pc import theme_server

ROOT = Path(__file__).resolve().parents[2]
GEN = ROOT / "tools" / "gen_theme_builtins.py"
SRC = ROOT / "firmware" / "src"
PAGE = ROOT / "pc" / "theme_builder.html"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _gen():
    return _load("gen_theme_builtins", GEN)


def _c(name):
    return (SRC / name).read_text(encoding="utf-8")


# ------------------------------------------------------------- one source

def test_the_committed_module_is_what_the_generator_writes():
    assert (ROOT / "pc" / "theme_builtins.py").read_text(
        encoding="utf-8") == _gen().source(), (
        "pc/theme_builtins.py differs from firmware/src/theme.c: run"
        " tools/gen_theme_builtins.py")


def test_the_generator_reads_every_role_it_needs_from_every_built_in():
    gen = _gen()
    pals = gen.palettes(_c("theme.c"), _c("theme.h"))
    assert len(pals) == 12
    for key, p in pals.items():
        for role in gen.ROLES:
            assert re.fullmatch(r"[0-9a-f]{6}", p[role]), (key, role)
        assert p["arc_w"] in (5, 8, 12, 22), key


def test_the_generator_refuses_a_table_with_a_theme_missing():
    gen = _gen()
    c_src = _c("theme.c").replace("[THEME_FOREST] =", "[THEME_FOREST_X] =")
    try:
        gen.builtins(_c("theme.h"), c_src)
    except ValueError as e:
        assert "FOREST" in str(e)
    else:
        raise AssertionError("a missing palette was not noticed")


# ------------------------------------------------------------- ids and names

def test_theme_ids_agree_between_the_firmware_and_the_computer():
    # theme.h's enum is the contract (append only); NAMES must be it.
    ids = _gen().enum_ids(_c("theme.h"))
    assert [n.lower() for _, n in ids] == list(theme.NAMES)
    assert theme.CUSTOM == dict((n, i) for i, n in ids)["CUSTOM"]
    assert theme_builtins.CUSTOM_ID == theme.CUSTOM
    assert [b["id"] for b in theme_builtins.BUILTINS] == list(
        range(theme.CUSTOM))
    assert [b["key"] for b in theme_builtins.BUILTINS] == list(
        theme.NAMES[:theme.CUSTOM])


def test_the_bench_tool_numbers_the_tiles_the_same_way():
    bt = _load("board_touch", ROOT / "tools" / "board_touch.py")
    assert bt.THEME_IDS == {n: i for i, n in enumerate(theme.NAMES)}


def test_every_theme_is_named_as_its_board_tile_names_it():
    names = _gen().board_names(_c("theme.c"))
    ids = _gen().enum_ids(_c("theme.h"))
    for tid, key in ids:
        assert theme.board_name(tid) == names[key], key


def test_blink_theme_list_shows_each_number_typed_name_and_tile_name(capsys):
    assert cli.main(["theme", "list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    names = _gen().board_names(_c("theme.c"))
    for tid, key in _gen().enum_ids(_c("theme.h")):
        row = [l for l in lines if re.match(rf"^ [ *] +{tid}  ", l)]
        assert len(row) == 1, (tid, lines)
        assert re.match(rf"^ [ *] +{tid}  {re.escape(theme.display_name(tid))}"
                        rf" +{re.escape(names[key])}\b", row[0]), row[0]


def test_every_number_and_tile_name_the_list_shows_is_one_set_takes():
    for tid in range(theme.CUSTOM):
        assert theme.from_args([str(tid)], None) == {"id": tid}
        assert theme.from_args([theme.board_name(tid)], None) == {"id": tid}
        assert theme.from_args([theme.display_name(tid)], None) == {"id": tid}


# ------------------------------------------------------------- the builder

def _served():
    page = theme_server.page()
    line = next(l for l in page.splitlines() if "var BUILTINS = " in l)
    return json.loads(line.split("var BUILTINS = ", 1)[1].rstrip().rstrip(";"))


def test_the_builder_previews_every_built_in_in_the_boards_own_colours():
    want = _gen().builtins(_c("theme.h"), _c("theme.c"))
    got = _served()
    assert len(got) == len(want) == 12
    for g, w in zip(got, want):
        for k in ("id", "name", "c", "w", "ends", "dot", "by"):
            assert g[k] == w[k], (w["name"], k, g[k], w[k])


def test_the_builder_page_types_no_palette_of_its_own():
    # The copy that drifted: any 6-digit hex literal in the page's script
    # is a colour that did not come from theme.c.
    html = PAGE.read_text(encoding="utf-8")
    script = html[html.index("<script>"):]
    assert not re.findall(r"'[0-9A-Fa-f]{6}'", script)


def test_a_custom_theme_started_from_nothing_is_dark_as_the_board_draws_it():
    dark = _gen().builtins(_c("theme.h"), _c("theme.c"))[0]
    assert theme.DEFAULT_CUSTOM == {"id": theme.CUSTOM, "c": dark["c"],
                                    "w": dark["w"], "ends": dark["ends"],
                                    "dot": dark["dot"], "by": dark["by"]}


def _lum(h):
    def lin(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(int(h[i:i + 2], 16)) for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _cr(a, b):
    la, lb = _lum(a), _lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def _dist(a, b):
    return math.dist([int(a[i:i + 2], 16) for i in (0, 2, 4)],
                     [int(b[i:i + 2], 16) for i in (0, 2, 4)])


def test_no_built_in_earns_a_warning_from_the_builders_own_checks():
    """The builder's Checks, applied to the presets it offers. The shipped
    Light ink drew "Two levels look almost the same" (its low and mid were
    both black), and Mono would have too: our own checker flagging our own
    theme. The thresholds are read out of the page so a change there is a
    change here."""
    html = PAGE.read_text(encoding="utf-8")
    # The three rules, as the page writes them; if one of these stops
    # matching, read checks() in the page and update this test with it.
    assert "dist(st[p[0]], st[p[1]]) < 60" in html
    assert "contrast(st[k], st.bg) < 2" in html
    assert "c >= 4.5 ? ['ok'" in html
    for b in _served():
        bg, text, g1, g2, low, mid, high = b["c"]
        assert _cr(text, bg) >= 4.5, (b["name"], "text")
        used = (low, mid, high) if b["by"] == "level" else (g1, g2, high)
        for c in used:
            assert _cr(c, bg) >= 2, (b["name"], c)
        if b["by"] == "level":
            for x, y in ((low, mid), (mid, high), (low, high)):
                assert _dist(x, y) >= 60, (b["name"], x, y)
