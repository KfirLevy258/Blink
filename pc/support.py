"""`blink support`: one zip a non-technical owner can send us (issue #20).

The Support section used to ask the owner to find ~/.blink/bridge.log and
paste `blink status` by hand, which is where a support exchange usually
stalled. This collects both, plus the versions, into one file on the Desktop
and says what it collected and where, so the owner can open it and look
before sending it.

What goes in is cut by an allowlist, not by a list of things to remove.
bridge.log is written through the redacting paths today (protocol.log_name,
logbook.console_line), but until 2026-10-30 both printed whole messages,
ids and usage figures into it, and a log rotated before that date can still
be on a customer's disk. A filter that removes the shapes we can think of
(UUIDs, hex, braces) passes the shapes we did not: an SSID printed bare, a
session name, "session 42%". So the rule is the other way round. A log line
is kept only if it starts with one of our own tags; every token in it is
kept only if it is a plain word or a version number, and anything else --
a digit, a path, a quote, an "=" value, an address, mixed case -- becomes a
fixed placeholder. A line that is not ours (a raw message dump, a legacy
echo of the board's console, a traceback's message) is dropped and counted.
`blink status` is cut the same way twice over: only the rows it is known to
print are kept (STATUS_LABELS; never the Wire row, which is the usage
message itself), and each row goes through the token rule, because its rows
carry port names, board ids and usage figures beside the words support
needs. A plain word can still be an id ("deadbeefcafe", a board name that
happens to be letters), so a word is kept only if it is one our own code
prints (vocabulary(): the words in pc/'s compiled literals), and a version
number is kept only in status.txt and system.txt, after a word that names
a version. bridge.log keeps no number at all.

The bundle never opens a serial port and never touches the service: it reads
files and runs `blink status`, which itself only reads. Nothing here logs
what it read, and nothing resolves home at import (tests/test_sandbox.py).
Standard library only, like everything in pc/.
"""
import functools
import getpass
import os
import platform
import re
import time
import zipfile

# The tags our own lines start with, in claude_usage_bridge.py and pc/
# (counted 2026-11-24 over every "[tag]" literal), and the board's console
# tags that logbook.console_line can leave bare. A line under any other tag
# is not one we can vouch for.
LOG_TAGS = frozenset({
    "bridge", "wifi", "ota", "update", "cfg", "usage", "proto", "theme",
    "burn", "settings", "fatal", "boot", "ask", "status", "ingest", "logo",
    "wsl", "watchdog", "plan", "slide", "sleep", "mem", "log", "touch",
    "time", "tap", "swipe", "spend", "focus", "bl", "board", "service",
})

# How much of the log goes in: the newest 256 KB, which is about ten hours
# of a busy desk at the 24 KB/hour logbook.CAP_BYTES measured. Support reads
# the last few hours; the whole 20 MB of history is too big to email.
LOG_CAP_BYTES = 256 * 1024
LOG_FILES = ("bridge.log.1", "bridge.log")    # oldest first

MASK = "<x>"
PATH_MASK = "<path>"

_STAMP = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) ")
_TAG = re.compile(r"^\[([a-z_-]+)\](?=\s|$)")
# A plain word: letters only, at most a leading capital, optionally joined
# by hyphens or an apostrophe ("not-found", "doesn't"), and not so long that
# it could be a key or a token in disguise. Fourteen letters a part is
# longer than any word our own lines use ("configuration" is thirteen).
_WORD = re.compile(r"^[A-Za-z][a-z]{0,13}(?:['-][a-z]{1,14}){0,3}$")
# An all-capitals word as the board and the daemon print them (HOST LOST,
# STALE, OTA, USB), short enough that it is a word and not a code.
_CAPS = re.compile(r"^[A-Z]{2,8}$")
# A dotted version, three or four parts: app, firmware, Claude Code, Python.
# Two parts are not allowed -- "1.50" is as likely to be a cost. Even three
# parts are kept only straight after a word that names a version (see
# VERSION_AFTER), because "42.10.5" on its own could be anything.
_VERSION = re.compile(r"^v?\d{1,4}(?:\.\d{1,4}){2,3}$")
# Only these words name a version, and only in status.txt and system.txt,
# which our current code prints: "App 1.5.1", "Claude Code 2.1.318",
# "firmware 1.5.1", "Python 3.12.1". A looser list ("board", "update",
# "landed") let "[usage] board 1.02.03 percent" through with its figure
# (review, 2026-11-24). bridge.log keeps no number at all, versions included:
# a rotated log can hold lines from before the redaction, written by code we
# no longer have, and system.txt already carries every version support reads.
VERSION_AFTER = frozenset({"app", "version", "firmware", "code", "python"})
# Words the rows print that the word rule would mask: mixed-case product
# names, protocol names with no vowel, and platform.machine()'s answers. A
# fixed vocabulary, so nothing a person chose can be in it.
KNOWN = frozenset({
    "WiFi", "macOS", "OpenCode", "PATH", "HOME", "USB", "arm64", "x86_64",
    "AMD64", "ARM64", "aarch64", "i386", "i686", "x64", "WSL", "UTF-8",
    "HTTP", "TCP", "UDP", "NDJSON", "JSON", "CYD", "LVGL", "DTR", "RTS",
    "TLS", "MCU", "CPU", "PSK", "MCUboot", "launchd", "systemd",
})
_VOWEL = set("aeiouy")
_CONSONANT_RUN = re.compile(r"[b-df-hj-np-tv-xz]{5}")


def is_version(text: str) -> bool:
    """A three- or four-part version number and nothing else."""
    return bool(_VERSION.match(text))


def _id_shaped(word: str) -> bool:
    """A word that passes the letter rule but reads as an id: only the
    letters a-f (a hex id with no digit in it, "deadbeefcafe"), no vowel at
    all ("qxzvbnmt"), or five consonants in a row ("tkrzwq"). Costs a few
    real words ("decade", "strengths"); those come out as <x>."""
    for part in re.split(r"[^a-z]+", word.lower()):
        if len(part) >= 6 and set(part) <= set("abcdef"):
            return True
        if len(part) >= 4 and not (set(part) & _VOWEL):
            return True
        if _CONSONANT_RUN.search(part):
            return True
    return False
_FLAG = re.compile(r"^--?[a-z][a-z-]{0,30}$")
_ARROWS = frozenset({"->", "<-", "--", "-", "=>", "|", ":", "/", "(", ")"})
_EDGE = "()[],.;:!?"


@functools.lru_cache(maxsize=1)
def vocabulary() -> frozenset:
    """Every word in the string constants of our own code: the modules of
    pc/ and the two entry points. A word the bundle keeps must be one of
    these, so whatever a person or a machine chose -- a board named
    "kelpfrond", a session title, a computer's name -- comes out as <x>
    however word-like it is. A shape rule alone could not do that: the
    review of 2026-11-24 found a word-shaped id it passed each round.

    The words are read from compiled code, not from source text, so a frozen
    build answers the same: its archive holds the bytecode and the bytecode
    holds every literal, f-string pieces and docstrings included. get_code()
    compiles without running anything; walk_packages imports only the
    package __init__ files, which do nothing (tests/test_import_is_inert.py).
    This repository is public, so nothing in the list is anyone's. If the
    code cannot be read at all the list is empty and every word is masked:
    a bundle that says too little rather than too much.
    """
    import importlib.util
    import pkgutil
    words = set()

    def walk(const):
        if isinstance(const, str):
            words.update(re.findall(r"[a-z]+", const.lower()))
        elif isinstance(const, (tuple, frozenset)):
            for c in const:
                walk(c)
        elif hasattr(const, "co_consts"):
            for c in const.co_consts:
                walk(c)

    names = ["claude_usage_bridge", "blink_main"]
    try:
        import pc
        names += [m.name for m in pkgutil.walk_packages(pc.__path__, "pc.")]
    except Exception:
        pass
    for name in names:
        if name == __name__:
            # This module's own literals include the id shapes its rules
            # describe ("deadbeefcafe"); they are not words to keep.
            continue
        try:
            spec = importlib.util.find_spec(name)
            code = spec.loader.get_code(name) if spec and spec.loader \
                else None
        except Exception:
            code = None
        if code is not None:
            walk(code)
    return frozenset(words)


def _token(tok: str, private=frozenset(), version_ok=False) -> str:
    """One whitespace-separated token as the bundle may hold it. A word
    survives only if every letter run in it is in vocabulary(), and a word
    in `private` (the account name, the computer's name) is masked even
    then. A version survives only when `version_ok`: the token before it
    named one."""
    if tok in _ARROWS:
        return tok
    core = tok.strip(_EDGE)
    if not core:
        return tok
    lead = tok[:len(tok) - len(tok.lstrip(_EDGE))]
    trail = tok[len(lead) + len(core):]
    # A command the status rows quote ("run `claude --version`") keeps its
    # backticks around whatever the rule leaves of the word inside.
    tick_l = "`" if core.startswith("`") else ""
    tick_r = "`" if core.endswith("`") and len(core) > 1 else ""
    inner = core[len(tick_l):len(core) - len(tick_r)]
    if not inner:
        return tok
    lead, trail = lead + tick_l, tick_r + trail

    vocab = vocabulary()

    def plain(w):
        if w in KNOWN:
            return True
        parts = [p for p in re.split(r"[^a-z]+", w.lower()) if p]
        return bool(_WORD.match(w) or _CAPS.match(w)) and \
            not _id_shaped(w) and \
            all(p in vocab and p not in private for p in parts)

    if plain(inner) or (version_ok and _VERSION.match(inner)) or \
            (_FLAG.match(inner) and plain(inner.lstrip("-"))):
        return lead + inner + trail
    # "USB/WiFi": words joined by a slash. A path has an empty part (a
    # leading slash) or a part that is not a plain word.
    if "/" in inner and all(plain(w) for w in inner.split("/")):
        return lead + inner + trail
    if "/" in inner or "\\" in inner or inner.startswith("~"):
        return lead + PATH_MASK + trail
    core = inner
    if "=" in core:
        key = core.split("=", 1)[0]
        if plain(key):
            return lead + key + "=" + MASK + trail
    return lead + MASK + trail


def scrub_text(text: str, private=frozenset(), versions=True) -> str:
    """Every token of `text` kept only if it is a plain word, a version
    named as one (never, with versions=False), or one of a few arrows; the
    rest masked. Spacing is kept, so the columns of `blink status` still
    line up."""
    out = []
    pos = 0
    prev = ""
    prev_version = False
    for m in re.finditer(r"\S+", text):
        tok = m.group(0)
        # "update 1.5.0 -> 1.5.1": an arrow carries a kept version on.
        ok = versions and (prev in VERSION_AFTER
                           or (prev == "->" and prev_version))
        kept = _token(tok, private, version_ok=ok)
        out.append(text[pos:m.start()])
        out.append(kept)
        pos = m.end()
        if tok in _ARROWS:
            # The arrow passes on whether the token before it was a kept
            # version.
            prev = tok
            continue
        prev_version = ok and kept == tok and bool(
            _VERSION.match(tok.strip(_EDGE)))
        prev = kept.strip(_EDGE + "`").lower() if kept == tok else ""
    out.append(text[pos:])
    return "".join(out)


# The rows `blink status` prints (pc/cli.py cmd_status and the helpers it
# calls), by the label in their first twelve columns. A row under any other
# label, and the Wire row above all (it is the usage message itself), is
# left out; a continuation line (twelve spaces) goes with the row above it.
STATUS_LABELS = (
    "Bridge", "Link", "App", "Board", "Claude Code", "Status line",
    "Activity", "Usage data", "Usage refresh", "Session", "Desktop",
    "Codex hook", "Codex",
)
_BOARD_ID = re.compile(r"\bid \S+?(?=,|$|\s)")


def status_rows(text: str, private=frozenset()):
    """`blink status` output cut to the rows support reads, each under
    scrub_text. Returns (text, dropped)."""
    kept, dropped, keeping = [], 0, False
    for line in text.splitlines():
        if not line.strip():
            continue
        if "{" in line or "}" in line:
            dropped += 1
            keeping = False
            continue
        if line.startswith(" " * 12):
            if keeping:
                kept.append(scrub_text(line, private))
            else:
                dropped += 1
            continue
        label = next((l for l in STATUS_LABELS
                      if line == l or line.startswith(l + " ")), None)
        if label is None:
            dropped += 1
            keeping = False
            continue
        keeping = True
        if label == "Board":
            # The board's own id, whatever shape it takes.
            line = _BOARD_ID.sub("id " + MASK, line)
        kept.append(scrub_text(line, private))
    return "".join(l + "\n" for l in kept), dropped


def redact_log_line(line: str, private=frozenset()):
    """One line of bridge.log as the bundle may hold it, or None to drop it.

    Kept: an optional logbook timestamp, then one of LOG_TAGS, then the
    words of the line under scrub_text. Dropped: everything else, which is
    a line that is not ours to vouch for -- untagged output, a raw message,
    a traceback, a line glued together from two writers.
    """
    text = line.rstrip("\r\n")
    stamp = ""
    m = _STAMP.match(text)
    if m:
        stamp, text = m.group(1) + " ", text[m.end():]
    t = _TAG.match(text)
    if not t or t.group(1) not in LOG_TAGS:
        return None
    rest = text[t.end():]
    # A tag with a message glued behind it ("[bridge] <- {'t': ...") is a
    # legacy dump: the token rule would mask its values one by one, but the
    # shape of the message itself is not ours to send either.
    if "{" in rest or "}" in rest:
        return None
    return stamp + t.group(0) + scrub_text(rest, private, versions=False)


def _read_tail(path, budget):
    """The last `budget` bytes of `path`, from a line boundary, as text."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > budget:
                f.seek(size - budget)
                data = f.read()
                cut = data.find(b"\n")
                data = data[cut + 1:] if cut >= 0 else b""
            else:
                data = f.read()
    except OSError:
        return None
    return data.decode("utf-8", errors="replace")


def log_tail(blink_dir, private=frozenset(), cap_bytes=LOG_CAP_BYTES):
    """The redacted tail of bridge.log and the generation before it.

    Returns (lines, dropped, found): the kept lines oldest first, how many
    lines were dropped, and whether any log file was there at all. The
    newest file gets the budget first; the older one has what is left.
    """
    texts = []
    budget = cap_bytes
    found = False
    for name in reversed(LOG_FILES):
        if budget <= 0:
            break
        text = _read_tail(os.path.join(blink_dir, name), budget)
        if text is None:
            continue
        found = True
        texts.append(text)
        budget -= len(text.encode("utf-8", errors="replace"))
    kept, dropped = [], 0
    for text in reversed(texts):
        for line in text.splitlines():
            if not line.strip():
                continue
            out = redact_log_line(line, private)
            if out is None:
                dropped += 1
            else:
                kept.append(out)
    return kept, dropped, found


def private_words(home):
    """Plain words that would pass the word rule but name this person or
    this machine: the account name, the home folder's name, the computer's
    name and its parts. Lower-cased, three letters or more (shorter would
    mask ordinary words)."""
    names = [os.path.basename(os.path.normpath(home))]
    try:
        names.append(getpass.getuser())
    except Exception:
        pass
    node = platform.node()
    names.append(node)
    names.extend(re.split(r"[^A-Za-z0-9]+", node))
    words = set()
    for n in names:
        words.update(w for w in re.split(r"[^a-z]+", n.lower())
                     if len(w) >= 3)
    # A computer named "MacBook-Pro" must not mask the word "pro" in every
    # line, nor "local" from "name.local".
    return frozenset(words - {"local", "lan", "home", "macbook", "pro",
                              "air", "mini", "desktop", "laptop", "users"})


def default_out_dir(home):
    """The Desktop, where an owner will find it; ~/.blink if there is none
    (a server, a Linux desktop without the folder)."""
    desktop = os.path.join(home, "Desktop")
    if os.path.isdir(desktop):
        return desktop
    return os.path.join(home, ".blink")


def show_path(path, home):
    """`path` with the home folder shown as ~, for what the owner reads."""
    home = os.path.normpath(home)
    p = os.path.normpath(path)
    if p == home or p.startswith(home + os.sep):
        return "~" + p[len(home):]
    return p


MANIFEST = """\
Blink support bundle, made by `blink support`.

  status.txt   the rows of `blink status` support reads
  system.txt   the app's and the board's versions, the operating system
               and the service state
  bridge.log   the newest part of the app's log

Every line was cut down before it was written here: words the app itself
prints are kept, and in status.txt and system.txt version numbers named as
versions; any other word, number, path or quoted value is shown as {mask}
or {path}. Log lines and status rows
that are not the app's own are left out ({dropped} of them this time).
This is a filter, not a proof: read the files before you send them.
"""


def build_bundle(out_dir, status_text, system_text, log_lines, dropped=0,
                 today=None):
    """Write the zip and return its path.

    Named for the day, with -2, -3 ... after it rather than over an earlier
    one: an owner who ran it twice may already have sent the first, or be
    looking at it. Created with O_EXCL so a race cannot overwrite either.
    """
    os.makedirs(out_dir, exist_ok=True)
    day = today or time.strftime("%Y-%m-%d")
    n = 1
    while True:
        name = f"blink-support-{day}.zip" if n == 1 else \
            f"blink-support-{day}-{n}.zip"
        path = os.path.join(out_dir, name)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | getattr(os, "O_BINARY", 0), 0o644)
        except FileExistsError:
            n += 1
            continue
        f = os.fdopen(fd, "wb")
        break
    with f, zipfile.ZipFile(f, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("MANIFEST.txt", MANIFEST.format(
            mask=MASK, path=PATH_MASK, dropped=dropped))
        z.writestr("status.txt", status_text)
        z.writestr("system.txt", system_text)
        z.writestr("bridge.log", "".join(l + "\n" for l in log_lines))
    return path
