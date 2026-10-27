"""Board themes, on the computer's side: what a theme is, the two files that
carry one to the daemon, and the daemon's reconciliation with the board.

The wire contract (orch CONTRACT-theme, 2026-10-27) in one paragraph: a
built-in theme is just its id; the custom one (id 12) carries seven colours
-- bg, text, gauge1, gauge2, low, mid, high -- an arc width of 5, 12 or 22,
round or flat ends, the dot on or off, and arcs coloured by level or per
gauge. The board ignores a line with any bad value whole, keeping its current
theme, which is safe and also silent: the person editing sees nothing change
and is told nothing. So every value is checked here, before a line is built,
and a refusal comes back as words the CLI and the builder page can show.

Two files, both under ~/.blink and both handed in as `home`, never resolved
here (tests/test_sandbox.py: a path computed at import reads the real ~):

  theme.json          the theme this computer wants the board to keep. The
                      daemon sends op=save when it changes, and after a hello
                      when the board reports a different one.
  theme_preview.json  written by the builder on every edit. The daemon's fast
                      tick sends it as op=preview (or op=cancel) and deletes
                      it, so a preview is sent once, never replayed.
  theme_sent.json     the last theme the board confirmed storing (theme_now
                      why=saved), as a fingerprint. Written by the daemon
                      only; it is what "the board holds" means before the
                      board has said anything on this connection.

Stdlib only, like the rest of pc/.
"""
import base64
import hashlib
import json
import os
import sys
import tempfile

from pc import protocol

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

# The custom theme `blink theme set k=v` starts from when none is saved: the
# builder's Dark preset, so editing one colour gives a theme that differs from
# what is on screen by that colour and nothing else.
DEFAULT_CUSTOM = {"id": CUSTOM,
                  "c": ["0e1116", "e6e8eb", "4387df", "0da243",
                        "0da243", "ba8107", "ff1900"],
                  "w": 12, "ends": "round", "dot": 1, "by": "level"}

SHARE_PREFIX = "BT1."
# A real code is about 140 characters. Anything far longer is not one, and is
# refused before it is decoded rather than after.
SHARE_MAX_CHARS = 400

THEME_FILE = "theme.json"
PREVIEW_FILE = "theme_preview.json"
SENT_FILE = "theme_sent.json"

# Why the board sent a theme_now (CONTRACT-theme, Addendum 2). Only "user" --
# the owner picking a theme in Settings on the board -- is a choice the
# computer adopts. "boot" reports what the board holds, and "saved" confirms
# a save this computer sent; adopting either would let a board that missed
# a save, or simply rebooted, overwrite the theme somebody set here.
WHY_ADOPT = "user"


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
        raise ThemeError(f"text on background is {ratio:.2f}:1, and the"
                         " board needs at least 3:1 to be readable")
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
    """The custom theme a share code holds, or ThemeError."""
    bad = ThemeError("that is not a theme share code (they start BT1.)")
    if (not isinstance(code, str) or not code.startswith(SHARE_PREFIX)
            or len(code) > SHARE_MAX_CHARS):
        raise bad
    body = code[len(SHARE_PREFIX):].strip()
    if not body:
        raise bad
    try:
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        fields = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise bad
    if not isinstance(fields, dict):
        raise bad
    return validate(dict(fields, id=CUSTOM))


# ------------------------------------------------------------- k=v

def _name_id(word):
    n = word.strip().lower().replace("-", "_")
    return NAMES.index(n) if n in NAMES else None


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
    never see half of one: temp file, then os.replace."""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".theme-", suffix=".tmp", dir=d)
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
            os.unlink(tmp)
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


def save(home, t, keep_custom=None):
    """Write theme.json. `keep_custom` (a custom theme) is stored beside a
    built-in id; a custom `t` is its own."""
    t = validate(t)
    obj = dict(t)
    if t["id"] != CUSTOM and keep_custom is not None:
        for k in CUSTOM_FIELDS:
            obj[k] = keep_custom[k]
    _write_json(theme_path(home), obj)
    return t


def forget(home):
    try:
        os.unlink(theme_path(home))
    except FileNotFoundError:
        pass


def write_preview(home, t):
    _write_json(preview_path(home), {"op": "preview", "theme": validate(t)})


def write_cancel(home):
    _write_json(preview_path(home), {"op": "cancel"})


def drop_preview(home):
    try:
        os.unlink(preview_path(home))
    except FileNotFoundError:
        pass


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

    Nothing here sends in reply to a message: Bridge sends what outgoing()
    returns on its ticks, and holds them off for one fast tick after a
    hello, so no theme line ever joins the hello burst (Addendum 2).
    """

    def __init__(self, home):
        self._home = home
        self._board = None
        self._asked = None
        sent = _read_json(sent_path(home))
        self._sent = _reported(sent)

    def hello(self):
        self._board = None
        self._asked = None

    def board_now(self, msg):
        """Record a theme_now. Adopts the theme only on why=user."""
        fp = _reported(msg)
        if fp is None:
            return
        why = msg.get("why")
        self._board = fp
        if why == "saved":
            self._sent = fp
            try:
                _write_json(sent_path(self._home), fp)
            except OSError as e:
                print(f"[theme] could not record the board's theme: {e}",
                      file=sys.stderr)
        elif why == WHY_ADOPT:
            self._adopt(fp)
        else:
            # "boot", or a word a newer board knows and we do not: a report,
            # never a choice. Whatever was asked before it may have been
            # lost to the reboot it announces.
            self._asked = None

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
            print(f"[theme] could not keep the board's choice: {e}",
                  file=sys.stderr)
        self._asked = None

    def outgoing(self, deliver=True):
        """Lines the files say are owed: a preview or cancel, then a save.

        With deliver False (a board that cannot take theme lines) the
        preview is still taken -- replaying it to the board later would show
        a theme somebody abandoned -- and the save stays owed.
        """
        out = []
        taken = take_preview(self._home)
        if taken is not None and deliver:
            op, t = taken
            out.append(message(op, t))
        want = load_saved(self._home)
        if not deliver or want is None:
            return out
        fp = fingerprint(want)
        holds = self._board if self._board is not None else self._sent
        if fp != holds and fp != self._asked:
            out.append(message("save", want))
            self._asked = fp
        return out
