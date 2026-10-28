#!/usr/bin/env python3
"""Write pc/theme_builtins.py from the firmware's theme table.

    PYTHONDONTWRITEBYTECODE=1 python3 tools/gen_theme_builtins.py

The board draws a built-in theme from firmware/src/theme.c and is told only
its id; the computer needs the same colours for one reason, the builder's
preview. It used to keep its own copy, typed into pc/theme_builder.html when
the builder was written, and the second round of firmware palettes changed
the board's colours without it: the audit of develop/fork-picks (2026-10-28)
found Colour-blind safe, Light, Light soft and Retro LCD previewing colours
the board never drew, and a custom theme started from one of those presets
starting from them too. Nothing failed, because nothing compared the two.

So there is one table, the firmware's, and this reads it. theme.c stays the
source because it is the one that has to be right -- it is what the desk
shows, and tests/theme checks it after RGB565 rounding -- and because the
frozen daemon cannot read a C file at run time, the result is committed as
a Python module rather than parsed on the customer's machine.
tests/pc/test_theme_builtins.py fails when the committed module and this
script disagree, so change theme.c and run this, never the output.

Standard library only, and deterministic: the same sources always give the
same bytes.
"""
import json
import os
import re
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
THEME_H = os.path.join(ROOT, "firmware", "src", "theme.h")
THEME_C = os.path.join(ROOT, "firmware", "src", "theme.c")
OUT = os.path.join(ROOT, "pc", "theme_builtins.py")

# A custom theme's seven colours, in the order they travel in "c"
# (pc/theme.py SLOTS), named by the struct palette roles they come from.
ROLES = ("bg", "text", "g1", "g2", "low", "mid", "high")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _strip_comments(src):
    return re.sub(r"/\*.*?\*/|//[^\n]*", "", src, flags=re.S)


def enum_ids(h_src):
    """theme.h's enum theme_id as [(id, "DARK"), ...], in id order."""
    body = re.search(r"enum theme_id\s*\{(.*?)\}", _strip_comments(h_src),
                     re.S)
    if body is None:
        raise ValueError("theme.h: no enum theme_id")
    ids = [(int(n), name) for name, n in
           re.findall(r"THEME_(\w+)\s*=\s*(\d+)", body.group(1))]
    if [i for i, _ in ids] != list(range(len(ids))):
        raise ValueError("theme.h: theme ids are not 0, 1, 2, ... in order")
    return ids


def _defines(h_src):
    return {k: int(v) for k, v in
            re.findall(r"#define\s+(THEME_W_\w+)\s+(\d+)", h_src)}


def palettes(c_src, h_src):
    """theme.c's builtin[] as {"DARK": {"bg": "0e1116", ..., "arc_w": 12,
    "flat": False, ...}}."""
    src = _strip_comments(c_src)
    # From the table's own opening brace, or "[THEME_BUILTIN_N] = {" in
    # its declaration reads as an entry.
    start = src.index("{", src.index("builtin[THEME_BUILTIN_N]")) + 1
    table = src[start:src.index("};", start)]
    widths = _defines(h_src)
    out = {}
    for name, body in re.findall(r"\[THEME_(\w+)\]\s*=\s*\{(.*?)\}",
                                 table, re.S):
        out[name] = _fields(name, body, widths)
    return out


# One designated initialiser whose value is a single token: a hex colour, a
# number, true or false, or one of theme.h's THEME_W_* widths.
_FIELD = re.compile(r"\s*\.(\w+)\s*=\s*(0x[0-9A-Fa-f]{1,6}|\d+|true|false"
                    r"|THEME_W_\w+)\s*")


# The struct palette members that are not 0xRRGGBB colours.
_NOT_COLOURS = ("arc_w", "flat", "dot", "by_gauge")


def _fields(name, body, widths):
    """A palette's initialisers, each one read whole or refused.

    The first version took the first token after each `=`, so
    `.low = 0x0DA243 | 0` would have read as 0x0DA243 with no word said,
    and only `+ 0` on a width failed, as an int() error (review of
    fp/audit-fixes, NICE 2). The table is plain on purpose; an expression
    in it is a change this script has to be taught, not one to guess at."""
    fields = {}
    pieces = body.split(",")
    if pieces and not pieces[-1].strip():
        pieces = pieces[:-1]            # the trailing comma
    for piece in pieces:
        m = _FIELD.fullmatch(piece)
        if m is None:
            raise ValueError(f"theme.c: THEME_{name}: cannot read"
                             f" {piece.strip()!r}; give each role a plain"
                             " value")
        key, val = m.groups()
        # A colour is six hex digits and nothing else is. `.bg = 0` reads
        # as a valid C zero, and was taken as the number 0 until a later
        # test's hex check failed on it (review of fp/audit-nice, NICE 3).
        if (key in _NOT_COLOURS) == bool(re.fullmatch(r"0x[0-9A-Fa-f]{6}",
                                                      val)):
            raise ValueError(f"theme.c: THEME_{name}: .{key} = {val} is not"
                             + (" a number or switch" if key in _NOT_COLOURS
                                else " a colour (0xRRGGBB)"))
        if val.startswith("0x"):
            fields[key] = "%06x" % int(val, 16)
        elif val in ("true", "false"):
            fields[key] = val == "true"
        elif val.startswith("THEME_W_"):
            if val not in widths:
                raise ValueError(f"theme.c: THEME_{name}: no {val} in"
                                 " theme.h")
            fields[key] = widths[val]
        else:
            fields[key] = int(val)
    return fields


def board_names(c_src):
    """theme.c's names[] as {"DARK": "Dark", ...}: what the tiles say."""
    src = _strip_comments(c_src)
    start = src.index("{", src.index("names[THEME_N]")) + 1
    table = src[start:src.index("};", start)]
    out = {}
    # Each entry whole, from its [THEME_X] to the next one: a findall for
    # the first literal read [THEME_FOREST] = "For" "est" as "For" and
    # never looked at the rest (review of fp/audit-nice, NICE 3).
    one = re.compile(r'\s*"((?:[^"\\\n]|\\.)*)"\s*,?\s*')
    heads = list(re.finditer(r"\[THEME_(\w+)\]\s*=", table))
    for i, h in enumerate(heads):
        key = h.group(1)
        end = heads[i + 1].start() if i + 1 < len(heads) else len(table)
        m = one.fullmatch(table[h.end():end])
        if m is None:
            raise ValueError(f"theme.c: the name of THEME_{key} is not one"
                             " plain string")
        name = m.group(1)
        # A tile name is plain printable ASCII (tests/test_panel_strings.py
        # holds the panel to that). An escape would need C's rules, not
        # this script's, to read, so it is refused rather than guessed.
        if "\\" in name or not all(" " <= ch <= "~" for ch in name):
            raise ValueError(f"theme.c: the name of THEME_{key} is not plain"
                             " ASCII")
        out[key] = name
    return out


def builtins(h_src=None, c_src=None):
    """Every built-in, in id order, as the module below carries it."""
    h_src = _read(THEME_H) if h_src is None else h_src
    c_src = _read(THEME_C) if c_src is None else c_src
    ids, pals, names = enum_ids(h_src), palettes(c_src, h_src), \
        board_names(c_src)
    out = []
    for tid, key in ids:
        if key == "CUSTOM":
            continue
        p = pals.get(key)
        if p is None:
            raise ValueError(f"theme.c: no palette for THEME_{key}")
        missing = [r for r in ROLES + ("arc_w",) if r not in p]
        if missing:
            raise ValueError(f"theme.c: THEME_{key} lacks {missing}")
        out.append({
            "id": tid, "key": key.lower(), "name": names[key],
            "c": [p[r] for r in ROLES], "w": p["arc_w"],
            # Omitted from a palette means false, as C initialisers do.
            "ends": "flat" if p.get("flat", False) else "round",
            "dot": 1 if p.get("dot", False) else 0,
            "by": "gauge" if p.get("by_gauge", False) else "level",
        })
    return out


def source():
    h_src, c_src = _read(THEME_H), _read(THEME_C)
    rows = builtins(h_src, c_src)
    custom = [tid for tid, key in enum_ids(h_src) if key == "CUSTOM"]
    lines = [
        '"""The board\'s built-in themes, as firmware/src/theme.c draws them.',
        "",
        "GENERATED by tools/gen_theme_builtins.py from firmware/src/theme.h",
        "and theme.c. Do not edit: change theme.c and run the script.",
        "tests/pc/test_theme_builtins.py fails when the two disagree.",
        "",
        "Each built-in is its id, its key (the enum name), the name its tile",
        "shows, and the seven colours and style a custom theme is made of,",
        "in the wire's field names and order (pc/theme.py CUSTOM_FIELDS).",
        "The board needs none of this -- a built-in travels by id alone --",
        "but the builder's preview, and a custom theme started from a",
        'built-in, need the colours the board really draws."""',
        "",
        "BUILTINS = (",
    ]
    # Every string through json.dumps, whose output for ASCII is also a
    # valid Python literal, so a quote or backslash in a name could never
    # write a different string than theme.c holds (review NICE 1).
    q = json.dumps
    for r in rows:
        lines.append(
            f'    {{"id": {r["id"]}, "key": {q(r["key"])},'
            f' "name": {q(r["name"])},')
        lines.append(
            '     "c": [' + ", ".join(q(c) for c in r["c"]) + "],")
        lines.append(
            f'     "w": {r["w"]}, "ends": {q(r["ends"])}, "dot": {r["dot"]},'
            f' "by": {q(r["by"])}}},')
    lines += [
        ")",
        "",
        f"CUSTOM_ID = {custom[0]}",
        f'CUSTOM_NAME = {json.dumps(board_names(c_src)["CUSTOM"])}',
        "",
    ]
    return "\n".join(lines)


def main():
    text = source()
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    print(f"wrote {os.path.relpath(OUT, ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
