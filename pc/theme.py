"""Board themes, on the computer's side: what a theme is, the files that
carry one to the daemon, and the daemon's reconciliation with the board.

The wire contract (orch CONTRACT-theme, 2026-10-27) in one paragraph: a
built-in theme is just its id; the custom one (id 12) carries seven colours
-- bg, text, gauge1, gauge2, low, mid, high -- an arc width of 5, 12 or 22,
round or flat ends, the dot on or off, and arcs coloured by level or per
gauge. The board ignores a line with any bad value whole, keeping its current
theme, which is safe and also silent: the person editing sees nothing change
and is told nothing. So every value is checked here, before a line is built,
and a refusal comes back as words the CLI and the builder page can show.

Four files, all under ~/.blink and all handed in as `home`, never resolved
here (tests/test_sandbox.py: a path computed at import reads the real ~):

  theme.json          the theme this computer wants the board to keep. The
                      daemon sends op=save when it changes, and after a hello
                      when the board reports a different one -- unless the
                      board changed it while the daemon was away, which
                      theme_seen.json tells apart (contract Addendum 4,
                      ThemeSync below). Every writer keeps the keys it does
                      not know, and one that exists but cannot be read is
                      never written over (Addendum 4a).
  theme_preview.json  written by the builder on every edit. The daemon's fast
                      tick sends it as op=preview (or op=cancel) and deletes
                      it, so a preview is sent once, never replayed.
  theme_sent.json     the last theme the board confirmed storing (theme_now
                      why=saved), as a fingerprint. Written by the daemon
                      only; it is what "the board holds" means before the
                      board has said anything on this connection.
  theme_seen.json     the last theme the daemon knew a board had, keyed by
                      the board's id, plus a save sent to it and not yet
                      confirmed. Written by the daemon only (Addendum 4a):
                      kept inside theme.json at first, it made every board
                      report a read-modify-write of the file `blink theme
                      set` and the builder write, and a set landing between
                      the read and the write was lost (review of fp/fix-pc,
                      S3).

`blink uninstall` removes every one of them, and their temp files, from
FILES below, so a file added here cannot be left behind by the uninstaller.

Stdlib only, like the rest of pc/.
"""
import base64
import hashlib
import json
import math
import os
import sys
import time

from pc import install_statusline
from pc import protocol
from pc import theme_builtins

# THEME_IDS, stable and append-only: the number is what cfg_store keeps, so a
# renumbering would repaint every board in the field the day it updated.
NAMES = ("dark", "neon", "bold", "mono", "ocean", "synthwave", "forest",
         "retro_lcd", "cvd_safe", "light", "light_soft", "light_ink",
         "custom")
CUSTOM = NAMES.index("custom")

# The seven colours, in the order they travel in "c".
SLOTS = ("bg", "text", "gauge1", "gauge2", "low", "mid", "high")
WIDTHS = {"thin": 5, "normal": 12, "thick": 22}
ENDS = ("round", "flat")
BY = ("level", "gauge")
OPS = ("preview", "save", "cancel")
CUSTOM_FIELDS = ("c", "w", "ends", "dot", "by")

# Text against background, below which the theme is refused. The plan sets
# 3:1 (revision 2026-10-25): the builder warns from 4.5:1, which is the WCAG
# floor for body text, but 3:1 is where the 12 px labels on this panel stop
# being legible at arm's length, and a theme that cannot be read is one the
# owner has to find Settings > Theme on to undo -- through that same text.
#
# 3.05 here, not 3.0, because the board checks the same ratio in its own
# arithmetic, and a theme that passes at 3.001 on this side and fails at
# 2.999 on that one would be accepted here, refused there, and the person
# editing would see nothing change (review, 2026-10-27). The margin puts
# every theme the computer accepts clear of the board's rounding.
MIN_CONTRAST = 3.05

# The custom theme `blink theme set k=v` starts from when none is saved:
# Dark, as the board draws it, so editing one colour gives a theme that
# differs from what is on screen by that colour and nothing else. Taken from
# pc/theme_builtins.py, which is generated from firmware/src/theme.c, rather
# than typed here: the builder's typed copy of the presets drifted from the
# board's for a whole firmware round before anyone saw it (audit, 2026-10-28).
DEFAULT_CUSTOM = dict(
    {k: theme_builtins.BUILTINS[0][k] for k in CUSTOM_FIELDS},
    id=CUSTOM, c=list(theme_builtins.BUILTINS[0]["c"]))

SHARE_PREFIX = "BT1."
# A real code is about 140 characters. Anything far longer is not one, and is
# refused before it is decoded rather than after.
SHARE_MAX_CHARS = 400

THEME_FILE = "theme.json"
PREVIEW_FILE = "theme_preview.json"
SENT_FILE = "theme_sent.json"
SEEN_FILE = "theme_seen.json"
# The port of the builder running on this home, if one is; see
# theme_server.running_builder.
BUILDER_FILE = "theme_builder.json"
# Every file this module writes under ~/.blink, for `blink uninstall`, which
# also takes their temp siblings. The ".taken" name is take_preview's claim,
# left behind by a daemon killed between its rename and its unlink.
FILES = (THEME_FILE, PREVIEW_FILE, PREVIEW_FILE + ".taken", SENT_FILE,
         SEEN_FILE, BUILDER_FILE)

# Why the board sent a theme_now (CONTRACT-theme, Addendum 2). "user" -- the
# owner picking a theme in Settings on the board -- is a choice the computer
# adopts. "saved" confirms a save this computer sent and is never adopted.
# "boot" reports what the board holds, and is adopted only when
# theme_seen.json says the board changed it while the daemon was away
# (Addendum 4, ThemeSync); adopting every one would let a board that missed a save, or
# simply rebooted, overwrite the theme somebody set here.
WHY_ADOPT = "user"
WHY_BOOT = "boot"


class ThemeError(ValueError):
    """A theme the board would refuse. The message is for a person."""


# ------------------------------------------------------------- colour

def _rgb(h):
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _luminance(h):
    def lin(v):
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(v) for v in _rgb(h))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    """WCAG contrast ratio of two "rrggbb" colours, 1.0 to 21.0."""
    la, lb = _luminance(a), _luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def _colour(v, what):
    if not isinstance(v, str) or len(v) != 6:
        raise ThemeError(f"{what} must be six hex digits, like 0e1116")
    try:
        int(v, 16)
    except ValueError:
        raise ThemeError(f"{what} must be six hex digits, like 0e1116")
    # int() also takes "+0e111" and " 0e111"; the board's parser does not.
    if not all(ch in "0123456789abcdefABCDEF" for ch in v):
        raise ThemeError(f"{what} must be six hex digits, like 0e1116")
    return v.lower()


# ------------------------------------------------------------- validation

def _theme_id(v):
    # bool is an int to Python and not to the board; 3.0 is a float to the
    # board's parser. Both are refused rather than coerced.
    if type(v) is not int or not 0 <= v < len(NAMES):
        raise ThemeError(f"theme id must be 0 to {len(NAMES) - 1}")
    return v


def validate(obj):
    """The theme as it may go to the board, or ThemeError.

    Returns a new dict holding only the contract's fields: {"id": N} for a
    built-in, and every custom field for id 12. Extra keys are dropped, which
    is what lets theme.json keep the custom colours beside a built-in id.
    """
    if not isinstance(obj, dict):
        raise ThemeError("a theme is a JSON object")
    tid = _theme_id(obj.get("id"))
    if tid != CUSTOM:
        return {"id": tid}
    missing = [k for k in CUSTOM_FIELDS if k not in obj]
    if missing:
        raise ThemeError("a custom theme needs " + ", ".join(missing))
    c = obj["c"]
    if not isinstance(c, list) or len(c) != len(SLOTS):
        raise ThemeError(f"a custom theme has {len(SLOTS)} colours: "
                         + ", ".join(SLOTS))
    colours = [_colour(v, name) for v, name in zip(c, SLOTS)]
    w = obj["w"]
    if type(w) is not int or w not in WIDTHS.values():
        raise ThemeError("width must be thin (5), normal (12) or thick (22)")
    if obj["ends"] not in ENDS:
        raise ThemeError("ends must be round or flat")
    dot = obj["dot"]
    if type(dot) is bool:
        dot = int(dot)
    if type(dot) is not int or dot not in (0, 1):
        raise ThemeError("dot must be on or off")
    if obj["by"] not in BY:
        raise ThemeError("arcs must be by-level or per-gauge")
    ratio = contrast(colours[1], colours[0])
    if ratio < MIN_CONTRAST:
        # Cut to two decimals, not rounded, and the floor named as it is
        # enforced: "3.03:1 ... at least 3:1" was over the floor it named
        # and under the one applied, and 3.0484 rounded to the 3.05 it
        # fell short of (review of 1.4.0, builder N2).
        shown = math.floor(ratio * 100) / 100
        raise ThemeError(f"text on background is {shown:.2f}:1, and the"
                         f" board needs at least {MIN_CONTRAST:.2f}:1 to be"
                         " readable")
    return {"id": CUSTOM, "c": colours, "w": w, "ends": obj["ends"],
            "dot": dot, "by": obj["by"]}


def message(op, t=None):
    """The wire line for `op`, validated and measured, or ThemeError.

    Measured with protocol.encode_checked rather than trusted to be short:
    the widest custom line is about 150 bytes today, and a field added later
    must not be the thing that finds the board's 512-byte cliff.
    """
    if op not in OPS:
        raise ThemeError(f"unknown theme op {op!r}")
    if op == "cancel":
        msg = protocol.theme("cancel")
    else:
        msg = protocol.theme(op, validate(t))
    raw, why = protocol.encode_checked(msg)
    if raw is None:
        raise ThemeError(why)
    return msg


# ------------------------------------------------------------- fingerprint

def theme_hash(t):
    """The board's "h" for a custom theme: the first 8 hex characters of the
    SHA-1 of its 26 bytes, in the order cfg_store keeps them (CONTRACT-theme,
    binding 2026-10-27): the id (12), the seven colours as R G B in "c"
    order, w as stored (5/12/22), ends (0 round, 1 flat), dot (0/1), by
    (0 level, 1 gauge).

    theme_now carries only an id, and every custom theme is id 12, so
    without this a change from one custom theme to another looked like no
    change at all and never reached the board (review, 2026-10-27).
    """
    t = validate(t)
    raw = (bytes([CUSTOM]) + bytes.fromhex("".join(t["c"]))
           + bytes([t["w"], ENDS.index(t["ends"]), t["dot"],
                    BY.index(t["by"])]))
    return hashlib.sha1(raw).hexdigest()[:8]


def fingerprint(t):
    """{"id": N} for a built-in, {"id": 12, "h": ...} for custom: what the
    board can say about a theme, so the only thing worth comparing."""
    t = validate(t)
    if t["id"] != CUSTOM:
        return {"id": t["id"]}
    return {"id": CUSTOM, "h": theme_hash(t)}


def _reported(msg):
    """The fingerprint a theme_now carries, or None for a bad one. A custom
    report with no usable hash keeps h None, which matches nothing -- the
    honest reading of "some custom theme"."""
    if not isinstance(msg, dict):
        return None
    try:
        tid = _theme_id(msg.get("id"))
    except ThemeError:
        return None
    if tid != CUSTOM:
        return {"id": tid}
    h = msg.get("h")
    ok = (isinstance(h, str) and len(h) == 8
          and all(ch in "0123456789abcdef" for ch in h))
    return {"id": CUSTOM, "h": h if ok else None}


# ------------------------------------------------------------- share code

def to_share_code(t):
    """"BT1." + base64url(JSON of the custom fields), no padding. None for a
    built-in theme, which has a name to share instead."""
    t = validate(t)
    if t["id"] != CUSTOM:
        return None
    body = json.dumps({k: t[k] for k in CUSTOM_FIELDS},
                      separators=(",", ":"))
    return SHARE_PREFIX + base64.urlsafe_b64encode(
        body.encode("ascii")).decode("ascii").rstrip("=")


def from_share_code(code):
    """The custom theme a share code holds, or ThemeError.

    Each refusal says what is wrong with the code. There was one message,
    "that is not a theme share code (they start BT1.)", and it was also
    the answer to `blink theme set BT1.garbage` -- a code that does start
    BT1. -- so it pointed at the one part that was right (audit,
    2026-10-28). The usual real cause is a copy that lost its end, and the
    builder page says the same words for the same cases."""
    if not isinstance(code, str) or not code.startswith(SHARE_PREFIX):
        raise ThemeError("that is not a theme share code (they start BT1.)")
    if len(code) > SHARE_MAX_CHARS:
        raise ThemeError("that share code is far too long to be one; a real"
                         " one is about 140 characters")
    damaged = ThemeError("that share code is damaged or cut short; copy all"
                         " of it again")
    body = code[len(SHARE_PREFIX):].strip()
    if not body:
        raise damaged
    try:
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        fields = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise damaged
    # An object with none of a theme's fields is no theme at all: "BT1.e30"
    # is "{}", which validate() answered with the list of fields it lacks
    # -- accurate, and of no use to someone holding a bad copy (review of
    # fp/audit-fixes, NICE 7).
    if not isinstance(fields, dict) or not any(
            k in fields for k in CUSTOM_FIELDS):
        raise damaged
    try:
        return validate(dict(fields, id=CUSTOM))
    except ThemeError as e:
        raise ThemeError(f"that share code holds a theme the board cannot"
                         f" use: {e}") from None


# ------------------------------------------------------------- k=v

def _key(word):
    return "_".join(word.strip().lower().replace("-", " ").split())


def _name_id(word):
    """A theme's id from what someone typed: its name as `blink theme list`
    prints it (retro-lcd), the name its tile shows on the board ("Retro
    LCD", "Colour-blind"), or its number in that list. The number is taken
    because the list prints it: a column that `set` then refused was the
    first thing the audit of 2026-10-28 tripped on."""
    w = word.strip()
    if w.isascii() and w.isdigit():
        return int(w) if int(w) < len(NAMES) else None
    n = _key(w)
    if n in NAMES:
        return NAMES.index(n)
    for tid in range(len(NAMES)):
        if _key(board_name(tid)) == n:
            return tid
    return None


def _onoff(v):
    v = v.lower()
    if v in ("on", "show", "1", "yes"):
        return 1
    if v in ("off", "hide", "0", "no"):
        return 0
    raise ThemeError("dot must be on or off")


def from_args(args, current):
    """`blink theme set ...` arguments to a validated theme.

    One bare name ("neon", "retro-lcd") picks a built-in. Otherwise every
    argument is key=value and they edit a custom theme: the one `current`
    holds if it has custom colours, else DEFAULT_CUSTOM.
    """
    args = list(args)
    if not args:
        raise ThemeError("say a theme name, a share code or key=value pairs")
    if len(args) == 1 and "=" not in args[0]:
        if args[0].startswith(SHARE_PREFIX):
            return from_share_code(args[0])
        tid = _name_id(args[0])
        if tid is None:
            raise ThemeError(f"no theme called {args[0]!r}; blink theme list"
                             " shows them")
        if tid == CUSTOM:
            raise ThemeError("custom is set with colours, for example"
                             " blink theme set bg=000000 text=ffffff")
        return {"id": tid}
    base = _custom_of(current) or DEFAULT_CUSTOM
    t = json.loads(json.dumps(base))
    t["id"] = CUSTOM
    for arg in args:
        key, sep, val = arg.partition("=")
        key = key.strip().lower()
        val = val.strip()
        if not sep or not val:
            raise ThemeError(f"{arg!r} is not key=value")
        if key in SLOTS:
            t["c"][SLOTS.index(key)] = _colour(val.lstrip("#"), key)
        elif key in ("width", "w"):
            w = WIDTHS.get(val.lower())
            if w is None and val.isdigit() and int(val) in WIDTHS.values():
                w = int(val)
            if w is None:
                raise ThemeError("width must be thin, normal or thick")
            t["w"] = w
        elif key == "ends":
            if val.lower() not in ENDS:
                raise ThemeError("ends must be round or flat")
            t["ends"] = val.lower()
        elif key == "dot":
            t["dot"] = _onoff(val)
        elif key in ("arcs", "by"):
            v = val.lower()
            by = {"by-level": "level", "level": "level",
                  "per-gauge": "gauge", "gauge": "gauge"}.get(v)
            if by is None:
                raise ThemeError("arcs must be by-level or per-gauge")
            t["by"] = by
        else:
            raise ThemeError(f"unknown setting {key!r}; the settings are "
                             + ", ".join(SLOTS)
                             + ", width, ends, dot and arcs")
    return validate(t)


def as_args(t):
    """The `blink theme set` arguments that make `t` (a custom theme)."""
    t = validate(t)
    if t["id"] != CUSTOM:
        return [display_name(t["id"])]
    width = {v: k for k, v in WIDTHS.items()}[t["w"]]
    return ([f"{k}={v}" for k, v in zip(SLOTS, t["c"])]
            + [f"width={width}", f"ends={t['ends']}",
               f"dot={'on' if t['dot'] else 'off'}",
               f"arcs={'by-level' if t['by'] == 'level' else 'per-gauge'}"])


def display_name(tid):
    """How a person types it: retro-lcd, not retro_lcd."""
    return NAMES[tid].replace("_", "-")


def board_name(tid):
    """What the theme's tile says on the board: "Colour-blind", "Light ink".
    The builder and `blink theme list` and `show` use it too, so a theme
    has one name wherever it is shown (audit D3: the builder said "Colour-
    blind safe" beside a board that said "Colour-blind")."""
    if tid == CUSTOM:
        return theme_builtins.CUSTOM_NAME
    return theme_builtins.BUILTINS[tid]["name"]


# ------------------------------------------------------------- files

def theme_path(home):
    return os.path.join(home, THEME_FILE)


def preview_path(home):
    return os.path.join(home, PREVIEW_FILE)


def sent_path(home):
    return os.path.join(home, SENT_FILE)


def _write_json(path, obj):
    """Whole or not at all. The daemon reads these files on a two-second
    tick while the builder writes them on every keystroke, so a reader must
    never see half of one: temp file, then os.replace.

    The temp file comes from install_statusline._open_temp, not
    tempfile.mkstemp. On Python 3.11, the interpreter the Windows build
    ships, mkstemp reads PermissionError as "that name is taken" and tries
    the next name, up to os.TMP_MAX, about two billion on Windows. In a
    ~/.blink where antivirus or a folder policy refuses new files, that hung
    the daemon's serial thread inside a theme_now: pongs stopped and the
    panel went to HOST LOST (review S4, 2026-10-28). _open_temp retries only
    a name that is really taken, ten times. The sweep removes what a killed
    write left, and the names are the ones install_statusline.temp_siblings
    finds, which is how `blink uninstall` takes them too.
    """
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    install_statusline._sweep_stale_temps(path)
    fd, tmp = install_statusline._open_temp(path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, separators=(",", ":"))
        # Retried on Windows' WinError 5 / 32 only, the way install_statusline
        # writes settings.json: the daemon reading theme.json on its tick, an
        # editor or an antivirus scan holding it for a moment, is not a failed
        # save. Imported here because pc.update is the heavier module and
        # the CLI's other paths do not need it.
        from pc import update
        update._replace(tmp, path, posix_retry=False)
    except BaseException:
        try:
            install_statusline._remove(tmp)
        except OSError:
            pass
        raise


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _custom_of(obj):
    """The custom fields in `obj`, valid, as a custom theme -- or None.

    theme.json keeps them beside a built-in id, so a board that goes from
    Custom to Neon and back is given its own colours again.
    """
    if not isinstance(obj, dict):
        return None
    try:
        return validate(dict(obj, id=CUSTOM))
    except ThemeError:
        return None


def saved_custom(home):
    """The custom colours theme.json holds, even beside a built-in id."""
    return _custom_of(_read_json(theme_path(home)))


def load_saved(home):
    """The theme this computer wants on the board, or None.

    None for no file and for a file that does not hold a valid theme: an
    unreadable choice is no choice, and sending a guess would repaint the
    board with something the owner never picked.
    """
    try:
        return validate(_read_json(theme_path(home)))
    except ThemeError:
        return None


class Unreadable(OSError):
    """theme.json is there and could not be read, so it must not be written
    over. The message names the file and the kind of failure, never what the
    file holds."""


def _read_existing(path):
    """theme.json as a dict before it is rewritten: {} when there is no
    file, and Unreadable for anything else that is not a JSON object.

    Only FileNotFoundError means "empty" (Addendum 4a). _read_json reads
    every failure as None, which is right for a reader and wrong for a
    writer: an antivirus scan or a backup tool holding the file without read
    sharing, a OneDrive placeholder that did not download, EACCES, EIO, a
    hand edit with a typo -- each read as empty, and the write that followed
    replaced the owner's theme and custom colours (review of fp/fix-pc, S2,
    reproduced with chmod 0 on macOS).
    """
    try:
        with open(path, encoding="utf-8") as f:
            obj = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise Unreadable(f"{path} is there but could not be read"
                         f" ({type(e).__name__}); it was left as it is."
                         " Fix it or delete it") from None
    if not isinstance(obj, dict):
        raise Unreadable(f"{path} does not hold a theme; it was left as it"
                         " is. Fix it or delete it")
    return obj


def read_saved_file(home):
    """theme.json as a dict ({} when there is none), or Unreadable when it is
    there and cannot be read. For a reader that must tell "nothing set" from
    "could not look"."""
    return _read_existing(theme_path(home))


def _kept_keys(obj):
    """What theme.json holds besides a theme: whatever a newer daemon adds,
    which the contract says every writer keeps (Addendum 4). A "seen" that
    a development build left here is kept too, and read by nobody."""
    theme_keys = ("id",) + CUSTOM_FIELDS
    return {k: v for k, v in obj.items() if k not in theme_keys}


def save(home, t, keep_custom=None):
    """Write theme.json. `keep_custom` (a custom theme) is stored beside a
    built-in id; a custom `t` is its own. Keys that are not a theme's are
    carried over. Unreadable (an OSError) when the file is there and cannot
    be read: it is never written over."""
    t = validate(t)
    obj = _kept_keys(_read_existing(theme_path(home)))
    obj.update(t)
    if t["id"] != CUSTOM and keep_custom is not None:
        for k in CUSTOM_FIELDS:
            obj[k] = keep_custom[k]
    _write_json(theme_path(home), obj)
    return t


def forget(home):
    """Stop holding a theme. Keys that are not a theme's stay; with none,
    the file goes. Unreadable when it is there and cannot be read."""
    kept = _kept_keys(_read_existing(theme_path(home)))
    if kept:
        _write_json(theme_path(home), kept)
        return
    try:
        os.unlink(theme_path(home))
    except FileNotFoundError:
        pass


# ------------------------------------------------------------- what was seen

def seen_path(home):
    return os.path.join(home, SEEN_FILE)


def _fp_of(obj):
    """A fingerprint stored in theme_seen.json, or None: a custom one without
    a usable hash is not one, because it would match nothing honestly."""
    fp = _reported(obj)
    if fp is None or (fp["id"] == CUSTOM and fp["h"] is None):
        return None
    return fp


def load_seen(home):
    """theme_seen.json as {"board_id", "fp", "asked", "kept", "lost"}, or
    None when it is absent or not one to trust.

    board_id/fp  what the daemon last knew that board had (Addendum 4).
    asked        a save sent to that board and not yet confirmed (review of
                 fp/fix-pc, N1), so a boot report of it reads as our own save
                 having landed, not as the board's choice.
    kept/lost    a boot report adopted over a theme set here while the two
                 were apart: what the board kept, and what this computer had.
                 `blink theme show` says so while theme.json is still `kept`.

    A record that does not parse is no record, and the rule for no record is
    the rule from before Addendum 4 (theme.json wins), so a damaged one can
    cost the owner a choice made on the board, never the sync itself. This
    file is the daemon's alone and holds nothing of the owner's, so it is
    simply rewritten.
    """
    obj = _read_json(seen_path(home))
    if not isinstance(obj, dict):
        return None
    bid = obj.get("board_id")
    if not isinstance(bid, str) or not bid:
        return None
    fp = _fp_of(obj)
    if fp is None:
        return None
    return {"board_id": bid, "fp": fp,
            "asked": _fp_of(obj.get("asked")),
            "kept": _fp_of(obj.get("kept")),
            "lost": _fp_of(obj.get("lost"))}


def _seen_obj(rec):
    """A load_seen() record as it is stored (contract Addendum 4a: board_id,
    id, h; the rest are additions a reader ignores)."""
    obj = dict(rec["fp"], board_id=rec["board_id"])
    for k in ("asked", "kept", "lost"):
        if rec.get(k) is not None:
            obj[k] = rec[k]
    return obj


def _write_seen(home, rec):
    """Write theme_seen.json, or remove it when rec is None. Only when it
    changes: the daemon calls this on every theme_now."""
    old = load_seen(home)
    if rec == old:
        return
    if rec is None:
        try:
            os.unlink(seen_path(home))
        except FileNotFoundError:
            pass
        return
    _write_json(seen_path(home), _seen_obj(rec))


def board_kept(home):
    """(kept, lost) fingerprints when the board's own choice was kept over a
    theme set on this computer while the two were apart, and theme.json is
    still that choice; else None. For `blink theme show` (review N6)."""
    rec = load_seen(home)
    if rec is None or rec["kept"] is None or rec["lost"] is None:
        return None
    want = load_saved(home)
    if want is None or fingerprint(want) != rec["kept"]:
        return None
    return rec["kept"], rec["lost"]


def write_preview(home, t):
    _write_json(preview_path(home), {"op": "preview", "theme": validate(t)})


def write_cancel(home):
    _write_json(preview_path(home), {"op": "cancel"})


def drop_preview(home):
    try:
        os.unlink(preview_path(home))
    except FileNotFoundError:
        pass


def builder_path(home):
    return os.path.join(home, BUILDER_FILE)


def write_builder(home, port):
    _write_json(builder_path(home), {"port": port})


def read_builder(home):
    """The port a builder recorded, or None."""
    obj = _read_json(builder_path(home))
    port = obj.get("port") if isinstance(obj, dict) else None
    if isinstance(port, int) and not isinstance(port, bool) and \
            0 < port < 65536:
        return port
    return None


def remove_builder(home, port):
    """Take the record away, but only a record of `port`: a builder that
    ends after a newer one has written its own must not delete that one."""
    if read_builder(home) != port:
        return
    try:
        os.unlink(builder_path(home))
    except OSError:
        pass


def board_takes_themes(home):
    """(takes, fw): whether the board this computer last saw takes theme
    lines, from the caps it listed (board.json, written by the daemon), not
    its version number: see protocol.BOARD_CAP_THEME. takes is None when
    that is not known -- no board.json, or one written by a daemon from
    before caps existed -- because a guess is worse than nothing here. fw is
    the version string it recorded, or None.

    Read by `blink theme` for its note and by the builder's /api/state: the
    daemon sends theme lines only to a board whose caps list "theme", so
    every preview to a 1.3.4 board is taken from the file and dropped, and a
    page saying "Preview sent" over that is false (review of 1.4.0,
    builder S3)."""
    try:
        with open(os.path.join(home, "board.json"), encoding="utf-8") as f:
            known = json.load(f)
        fw, caps = known.get("fw"), known.get("caps")
    except (OSError, ValueError, AttributeError):
        return None, None
    fw = fw if isinstance(fw, str) and fw else None
    if not isinstance(caps, list):
        return None, fw
    return protocol.BOARD_CAP_THEME in caps, fw


def preview_waiting_s(home):
    """Seconds the preview file has been waiting for the daemon to take it,
    or None when there is no file. The daemon takes it on its fast tick,
    two seconds (claude_usage_bridge.FAST_POLL_INTERVAL_S), so a file that
    has waited well past that has no daemon, or no board, behind it."""
    try:
        return max(0.0, time.time() - os.path.getmtime(preview_path(home)))
    except OSError:
        return None


def take_preview(home):
    """("preview", theme), ("cancel", None) or None. Takes the file.

    Claimed by renaming it first, then read: the builder writes a new
    preview on every edit, and one written between our read and our delete
    would otherwise be deleted unsent. After the rename a new write lands
    under the real name, for the next tick.
    """
    src = preview_path(home)
    claimed = src + ".taken"
    try:
        os.replace(src, claimed)
    except OSError:
        return None
    obj = _read_json(claimed)
    try:
        os.unlink(claimed)
    except OSError:
        pass
    if not isinstance(obj, dict):
        return None
    if obj.get("op") == "cancel":
        return ("cancel", None)
    if obj.get("op") == "preview":
        try:
            return ("preview", validate(obj.get("theme")))
        except ThemeError:
            return None
    return None


# ------------------------------------------------------------- the daemon

class ThemeSync:
    """What the daemon owes the board, theme-wise.

    Three facts, and the files they live in:
      want   theme.json -- what this computer wants the board to keep.
      board  the last theme_now heard on this run, as a fingerprint.
      sent   theme_sent.json -- the last theme the board CONFIRMED storing.
    The board is told to save `want` when it differs from what the board
    holds: `board` once the board has spoken, `sent` before that. Assuming
    the board already held theme.json, as this did at first, meant a theme
    set while the daemon was down never reached the board at all.

    One save per difference. `asked` remembers the save already sent and
    not yet confirmed, so a tick that comes round before the board's
    why=saved answer does not send it again; a reboot (hello) or a boot
    report clears it, because a save the board had not stored yet is lost
    with the reboot and is owed again.

    A theme picked on the board while no daemon was listening (contract
    Addendum 4). Its why=user line went nowhere, so on reconnect all the
    daemon heard was a boot report that differed from theme.json, and it
    sent theme.json back: the owner's Neon became Ocean again, with no
    message (review S2, reproduced in a scratch home on 2026-10-28). So
    theme_seen.json keeps what the daemon last knew a board had, from any
    theme_now, keyed by the board's id (Addendum 4a). On a boot report from
    that same board:
      - the board still has what was seen: it did not change, so if
        theme.json differs it is this computer that changed, and the save
        goes out as before;
      - the board has the save we sent and never heard confirmed: our own
        save landed and its why=saved was lost (a dropped line, a daemon
        killed in the window). Not the board's choice -- adopting it would
        let this computer's earlier save beat its later one (N1);
      - the board has something else: it was changed on the board, so it is
        adopted, exactly as a why=user would have been, and nothing is sent;
      - no record, one that does not parse, or another board's: theme.json
        wins, as before Addendum 4, and the record starts from this report.
    When both sides changed while apart, the board wins: it is the change
    the owner made last that they can see, and the one they would have to
    make again by hand. The record says so, for `blink theme show`.

    The board's id is the caller's to give, and only from the wire: a hello,
    or a pref, which names the board from firmware 1.4.0 on (Addendum 5).
    An id known only from board.json is never passed. A board swapped while
    the daemon was off, whose hello went to nobody, would be read as the
    board board.json remembers, and its theme adopted over this computer's
    (review of fp/fix-pc, S1). With no id, nothing is compared or recorded:
    theme.json wins, as it did before Addendum 4, so a board whose pref does
    not name it is only ever adopted from after a hello.

    Nothing here sends in reply to a message: Bridge sends what outgoing()
    returns on its ticks, and holds them off for one fast tick after a
    hello, so no theme line ever joins the hello burst (Addendum 2).
    """

    def __init__(self, home):
        self._home = home
        self._board = None
        self._board_id = None
        self._asked = None
        # A board choice whose adopt was refused because theme.json could
        # not be read (Unreadable). Retried on every tick, and while it
        # stands no save goes out. Without it, the tick after the file came
        # back read the old theme, saw it differ from the board, and sent
        # it: the owner's pick reverted on the panel, with no message
        # (round-2 review of fp/fix-pc, R1, reproduced).
        #
        # Only the fast path within one connection. While it stands the seen
        # record is NOT moved to the board's pick, so after a reconnect or a
        # restart the boot report still differs from the record and the pick
        # is adopted again (round 3, R5: moved, the boot report read as "no
        # change" and the old theme was sent). _pending_note is what to
        # record once the adopt goes through: (board_id, why, kept, lost).
        # _pending_mtime is theme.json's mtime at the refusal: a file
        # written after it is the computer's newer choice, which the retry
        # must not overwrite (R6).
        self._pending = None
        self._pending_note = None
        self._pending_mtime = None
        sent = _read_json(sent_path(home))
        self._sent = _reported(sent)

    def hello(self):
        self._board = None
        self._board_id = None
        self._asked = None
        self._pending = None
        self._pending_note = None

    def _record_for(self, board_id):
        """theme_seen.json's record when it is about `board_id`, else None."""
        rec = load_seen(self._home)
        if rec is None or board_id is None or rec["board_id"] != board_id:
            return None
        return rec

    def board_now(self, msg, board_id=None):
        """Record a theme_now from the board `board_id`: an id off the wire,
        or None when this connection has not heard one.

        Adopts the theme on why=user, and on why=boot when the board's
        theme is neither the one last seen on that same board nor a save we
        sent it unconfirmed (Addendum 4, N1).
        """
        fp = _reported(msg)
        if fp is None:
            return
        if not isinstance(board_id, str) or not board_id:
            board_id = None
        if board_id is not None:
            self._board_id = board_id
        why = msg.get("why")
        self._board = fp
        if why in (WHY_ADOPT, WHY_BOOT):
            # The board has spoken again; what it says now decides, and
            # sets a new pending adopt below if that one is refused too.
            # The record was left where it was, so a boot report after a
            # reconnect still reads as the board's change (R5).
            self._pending = None
            self._pending_note = None
        rec = self._record_for(board_id)
        kept = lost = None
        if why == "saved":
            self._sent = fp
            try:
                _write_json(sent_path(self._home), fp)
            except OSError as e:
                print(f"[theme] could not record the board's theme: {e}",
                      file=sys.stderr)
        elif why == WHY_ADOPT:
            self._adopt(fp)
        elif (why == WHY_BOOT and rec is not None and _fp_of(fp) is not None
              and fp != rec["fp"] and fp != rec["asked"]):
            want = load_saved(self._home)
            mine = fingerprint(want) if want is not None else None
            if mine is not None and mine != rec["fp"] and mine != fp:
                # Both sides changed while apart. Said in the log, and kept
                # in the record so `blink theme show` can say it too.
                kept, lost = fp, mine
            # Says that it happened, never what: the id is a value off the
            # wire (CLAUDE.md, serial buffers). It ended "keeping the
            # board's" until the audit of 2026-10-28 read it as cut off.
            print("[theme] the theme was changed on the board while this app"
                  " was away; keeping the board's choice",
                  file=sys.stderr)
            self._adopt(fp)
        else:
            # "boot" from a board that did not change, or that holds the save
            # we sent it, or one with no record; or a word a newer board knows
            # and we do not: a report, never a choice. Whatever was asked
            # before it may have been lost to the reboot it announces.
            self._asked = None
        if self._pending is not None:
            # Refused: the record keeps what it had (R5), and is moved once
            # the adopt goes through.
            self._pending_note = (board_id, why, kept, lost)
            return
        self._note_seen(board_id, fp, why, rec, kept, lost)

    def _note_seen(self, board_id, fp, why, rec, kept, lost):
        """Keep theme_seen.json in step with the board. A custom report with
        no usable hash drops the record: what the board has is no longer
        known, and a stale record would let a later boot report pass for no
        change. Written only when it changes."""
        if board_id is None:
            return
        if _fp_of(fp) is None:
            new = None if rec is not None else load_seen(self._home)
        else:
            new = {"board_id": board_id, "fp": fp, "asked": None,
                   "kept": kept, "lost": lost}
            if rec is not None:
                # A save still unconfirmed stays owed only past a confirmation
                # of some other theme; a boot or a choice on the board ends it.
                if why == "saved" and rec["asked"] != fp:
                    new["asked"] = rec["asked"]
                # What the board kept over us is news until our next save
                # lands, or the owner picks again on the board. Carried past
                # a why=user, it came back stale the day the owner picked the
                # kept theme again (round-2 review, R4).
                if kept is None and why not in ("saved", WHY_ADOPT):
                    new["kept"], new["lost"] = rec["kept"], rec["lost"]
        try:
            _write_seen(self._home, new)
        except OSError as e:
            print(f"[theme] could not record the board's theme: {e}",
                  file=sys.stderr)

    def _note_asked(self, fp):
        """A save just went to the board: remember it until it is confirmed,
        in the record, so a daemon that restarts in between still knows."""
        if self._board_id is None or self._board is None:
            return
        rec = self._record_for(self._board_id)
        if rec is None:
            rec = {"board_id": self._board_id, "fp": self._board,
                   "asked": None, "kept": None, "lost": None}
            if _fp_of(self._board) is None:
                return
        rec = dict(rec, asked=fp)
        try:
            _write_seen(self._home, rec)
        except OSError as e:
            print(f"[theme] could not record the save sent: {e}",
                  file=sys.stderr)

    def _adopt(self, fp):
        kept = saved_custom(self._home)
        try:
            if fp["id"] != CUSTOM:
                save(self._home, fp, keep_custom=kept)
            elif kept is not None and theme_hash(kept) == fp["h"]:
                save(self._home, kept)
            else:
                # Custom colours this computer never had -- set from another
                # desk. They cannot be named here, so this computer stops
                # holding a theme that would overwrite them.
                forget(self._home)
        except OSError as e:
            if self._pending != fp:
                # Once per refusal, not once per retry tick.
                print(f"[theme] could not keep the board's choice: {e}",
                      file=sys.stderr)
                self._pending_mtime = self._theme_mtime()
            self._pending = fp
        else:
            self._pending = None
        self._asked = None

    def _theme_mtime(self):
        try:
            return os.stat(theme_path(self._home)).st_mtime_ns
        except OSError:
            return None

    def _retry_pending(self):
        """Retry a refused adopt; or drop it when theme.json was written
        after the refusal and now reads. That write is this computer's newer
        choice (a `blink theme set`, an editor's save), and the retry would
        have put the board's older pick over it without a word (R6,
        reproduced). Either way the record then moves to the board's pick:
        the board does have it, so the normal rule decides what is owed."""
        fp, note = self._pending, self._pending_note
        now = self._theme_mtime()
        rewritten = now is not None and now != self._pending_mtime
        if rewritten:
            try:
                _read_existing(theme_path(self._home))
            except Unreadable:
                rewritten = False
        if rewritten:
            self._pending = None
        else:
            self._adopt(fp)
        if self._pending is None and note is not None:
            board_id, why, kept, lost = note
            if rewritten:
                kept = lost = None
            self._note_seen(board_id, fp, why, self._record_for(board_id),
                            kept, lost)
            self._pending_note = None

    def outgoing(self, deliver=True):
        """Lines the files say are owed: a preview or cancel, then a save.

        With deliver False (a board that cannot take theme lines) the
        preview is still taken -- replaying it to the board later would show
        a theme somebody abandoned -- and the save stays owed.
        """
        out = []
        taken = take_preview(self._home)
        if self._pending is not None:
            self._retry_pending()
        if not deliver:
            return out
        save = None
        want = load_saved(self._home)
        # A refused adopt still standing: the file may read again before the
        # adopt goes through, and then it holds the theme the board left.
        if want is not None and self._pending is None:
            fp = fingerprint(want)
            holds = self._board if self._board is not None else self._sent
            if fp != holds and fp != self._asked:
                save = message("save", want)
                self._asked = fp
                self._note_asked(fp)
        # Save in the builder and `blink theme set` always leave a cancel,
        # because a Save of the theme already stored owes no save line, and
        # without the cancel the last preview stayed on the board for its
        # full 15 minutes while the page said "Saved" (review NICE-4). When
        # a save IS owed, the cancel is dropped: sent first, it would repaint
        # the old stored theme for a moment before the save, and the save
        # ends the board's preview on its own (ui_theme.c, THEME_OP_SAVE
        # calls end_preview).
        if taken is not None and not (taken[0] == "cancel" and save):
            op, t = taken
            out.append(message(op, t))
        if save is not None:
            out.append(save)
        return out
