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
session name, "session 42%".

The rule: a log line keeps its timestamp, its tag, and the one fixed phrase
it opens with, from that tag's list (PHRASES). Whatever follows the phrase
is shown as a single <x>, and a line that opens with no listed phrase is its
tag and <x>. A status row is its label and one phrase from STATUS_PHRASES
the same way. What is written is the phrase as it is typed below, never the
line's own text, so every word in a bundle is one a person put in this
file, and nothing past the opening phrase can show whatever it says.

That last part is the reason the rule is by position. Keeping every listed
word wherever it stood let a value through when the value was itself a
listed word: a board whose id is "running" showed in the Board row, and an
old "[wifi] joined network" showed a network called "network". A value
never opens one of our lines, so it is never inside the phrase. For the
same reason no phrase may run on past the end of another (the tests hold
this): with "on, but <count> failed" in the code, a listed "on, but Claude
Code wasn't found" would let the count's place spell words.

The tags the board's console uses (logbook.CONSOLE_TAGS) list only what the
daemon prints under them. The board's own lines are cut to the bare tag
before they are logged today, and here they are cut the same way when an
old log still has them whole.

What is left over: a line whose very first words after the tag were filled
in, and happen to spell a whole listed phrase, shows that phrase.

An earlier shape of this module matched every line against the exact
literal that printed it, 415 of them pasted here and re-derived by a test.
That made every change to a log line anywhere fail a test in this file. A
word list read from our own string constants at run time was tried before
that: it held 4,793 words, names among them.

Numbers are never kept, with two exceptions in status.txt: the version
straight after the App label and after the Claude Code label. A usage
figure can be shaped like a version, so no other place may hold one, and
continuation lines of `blink status` (the indented ones) are left out
whole. The firmware's version is in system.txt, from board.json.

The bundle never opens a serial port and never touches the service: it reads
files and runs `blink status`, which itself only reads. Nothing here logs
what it read, and nothing resolves home at import (tests/test_sandbox.py).
Standard library only, like everything in pc/.
"""
import os
import re
import sys
import time
import zipfile

from pc.protocol import UNKNOWN_TYPE, WIRE_TYPES

# Per tag, the phrases a log line may open with. The keys are every tag our
# lines open with: each "[tag]" that starts a string literal in
# claude_usage_bridge.py, blink_main.py and pc/, and the board's console
# tags (logbook.CONSOLE_TAGS). A line under any other tag is dropped, so
# tests/pc/test_support_cli.py derives the keys again from the source and
# fails when a tag was added and not listed here. Only the keys are held to
# the source: the phrases are a person's choice of what support reads a log
# for, each one the fixed opening of a line, ending before anything that is
# filled in. A tag with no phrases keeps its time and tag.
PHRASES = {
    "ask": (), "bl": (), "boot": (),
    "bridge": (
        "NOT SENT:", "another Blink daemon is already running",
        "Blink's config.json could not be read;", "board found at",
        "builder state unknown:", "connected on",
        "could not check the login service file:",
        "could not record the pid:", "link ok:", "log rotated;",
        "login service file brought up to date;", "logo:", "main source:",
        "no usage data yet", "nothing answered quickly;",
        "nothing here answers;", "ota: FAILED:", "ota: app update failed;",
        "ota: asset is", "ota: board has", "ota: board speaks",
        "ota: closing port to flash", "ota: could not resume",
        "ota: download failed:", "ota: flashing", "ota: no manifest yet;",
        "ota: not offering", "ota: offering",
        "ota: resuming the approved install of",
        "ota: sent; waiting for the board to restart", "ota: sha256",
        "ota: the release moved on; not resuming",
        "ota: updating this app to", "probe not sent:", "serial lost:",
        "spend burn line working again", "spend burn not sent:",
        "stopping once the", "the board speaks",
        "the login service file has been edited,",
        "the other daemon exited; taking over",
        "theme builder did not open:",
        "theme builder: no browser to open here;", "theme not sent:",
        "upkeep failed while waiting:", "waiting for the board"),
    "burn": (
        "a Codex sessions directory cannot be listed;",
        "a project directory cannot be listed;",
        "every Codex sessions directory lists again",
        "every project directory lists again",
        "the Claude Code projects folder cannot be listed;",
        "the Claude Code projects folder lists again",
        "the Codex sessions folder cannot be listed;",
        "the Codex sessions folder lists again"),
    "cfg": (), "claude": (), "desktop": (), "fatal": (), "focus": (),
    "ingest": (
        "Desktop data folder:", "Desktop folder lookup failed",
        "the board asked for a provider that is not reporting;"),
    "log": ("last message repeated",),
    "logo": (), "mem": (),
    "ota": ("could not read the release feed:",
            "could not read the chip's eFuses on"),
    "plan": (), "proto": (),
    "settings": (
        "logo encoder bug", "logo preview could not be read",
        "logo preview not kept", "request failed", "request file refused",
        "request served", "the board still reports its logo after remove"),
    "sleep": (), "slide": (),
    "spend": ("count not saved", "reading failed"),
    "status": ("check failed:",),
    "swipe": (),
    "tap": ("cannot write",),
    "test": (),
    "theme": (
        "builder could not start:", "builder ended:",
        "could not keep the board's choice:", "could not record",
        "the builder ended; told the board",
        "the theme was changed on the board"),
    "time": (), "touch": (),
    "update": (
        "cannot read", "check failed:", "could not read the release feed:",
        "could not record the pending update:",
        "could not start the new program:", "download failed:",
        "installing", "manifest signature did not verify",
        "restarting into the new version",
        "restored the previous program at", "running from a checkout:"),
    "usage": (), "watchdog": (),
    "wifi": (
        "could not remove the pairing file;", "could not save the pairing",
        "link lost", "the board could not erase its WiFi settings",
        "the board joined but did not answer over WiFi",
        "the board was set up from another computer",
        "the pairing file cannot be read,",
        "the pairing file is not owner-only,"),
    "wsl": (
        "check failed:", "could not check the distribution's name:",
        "the WSL distribution Blink was set up for is gone;"),
}
# "[bridge] -> usage": the direction and the type of each message, which is
# what support reads first. The types are protocol.WIRE_TYPES and the
# placeholder for anything else, the same fixed names protocol.log_name
# writes from.
PHRASES["bridge"] += tuple(
    way + " " + t for way in ("->", "<-")
    for t in sorted(WIRE_TYPES | frozenset((UNKNOWN_TYPE,))))
LOG_TAGS = frozenset(PHRASES)

# The rows `blink status` prints (pc/cli.py cmd_status and the helpers it
# calls), by the label in their first columns, each with the phrases the row
# may open with. A row under any other label, and the Wire row above all (it
# is the usage message itself), is left out. "Codex hook" stands before
# "Codex" because the first match is taken. The Bridge phrases are the
# service states, and system.txt's Service line is cut by them too.
STATUS_PHRASES = {
    "Bridge": (
        "running", "not running", "not installed", "not checked",
        "installed but not loaded", "registered but FAILING",
        "registered with launchd", "registered as a Scheduled Task",
        "registered to start when you log in", "no systemd here;",
        "unknown on"),
    "Link": (
        "USB cable", "no board connected", "unknown (app not reporting)",
        "WiFi, board at", "WiFi set up, board not found"),
    "App": (),
    "Board": ("not plugged in", "none identified yet",
              "could not list serial ports:"),
    "Claude Code": ("not found", "on PATH, but", "WSL"),
    "Status line": ("installed at", "installed inside", "not installed",
                    "inside"),
    "Activity": (
        "not checked", "unknown -- settings.json could not be read",
        "unknown -- settings.json does not parse", "hooks installed",
        "hooks not installed", "PARTIAL"),
    "Usage data": ("fresh", "none yet", "from Claude Desktop's own files",
                   "from the Codex session log"),
    "Usage refresh": (
        "off", "off: Claude Code's /usage used the model",
        "off: /usage kept failing;", "not set up",
        "not available for WSL yet", "unreadable", "on", "on, but",
        "on, last ran"),
    "Session": ("no five-hour window",),
    "Desktop": (
        "usage cache parsed, reading",
        "usage cache present but holds no usage",
        "usage cache present but could not be read",
        "usage cache present but did not parse", "no usage cache"),
    "Codex hook": (
        "not installed", "firing,",
        "registered, but its slots could not be read",
        "registered, but it has never written anything"),
    "Codex": ("session log parsed, reading", "no session logs"),
}
STATUS_LABELS = tuple(STATUS_PHRASES)
# The rows that may keep one version, as their first word: "App 1.5.1",
# "Claude Code 2.1.318".
VERSION_ROWS = frozenset(("App", "Claude Code"))

# How much of the log goes in: the newest 256 KB, which is about ten hours
# of a busy desk at the 24 KB/hour logbook.CAP_BYTES measured. Support reads
# the last few hours; the whole 20 MB of history is too big to email.
LOG_CAP_BYTES = 256 * 1024
LOG_FILES = ("bridge.log.1", "bridge.log")    # oldest first
# logbook.BOARD_LINE: what bridge.log says for a console line of the board
# that is not one of our printks. A constant, so it is kept whole.
BOARD_LINE = "(board line)"

MASK = "<x>"

SYSTEMS = frozenset(("Darwin", "Linux", "Windows"))
MACHINES = frozenset(("arm64", "x86_64", "AMD64", "ARM64", "aarch64",
                      "x86", "i386", "i686", "armv7l"))

_STAMP = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) ")
_TAG = re.compile(r"^\[([a-z_-]+)\](?=\s|$)")
# Three or four numeric parts: "1.5" is as likely a figure as a version.
_VERSION = re.compile(r"^v?\d{1,4}(?:\.\d{1,4}){2,3}$")


def is_version(text: str) -> bool:
    return bool(_VERSION.match(text))


def keep_phrase(text: str, phrases, version_first=False) -> str:
    """What `text` may show: the longest of `phrases` it opens with, written
    as the phrase is listed, then one mask if anything follows. A text that
    opens with none is the mask alone. Words are compared whole, punctuation
    included, without case: "usage=7" is not the word "usage", and "off:" is
    not "off". `version_first` keeps a version when it is the first word,
    and the phrase is looked for after it."""
    tokens = text.split()
    out = []
    if version_first and tokens and is_version(tokens[0]):
        out.append(tokens.pop(0))
    low = [t.lower() for t in tokens]
    best = []
    for phrase in phrases:
        want = phrase.lower().split()
        if len(want) > len(best) and low[:len(want)] == want:
            best = phrase.split()
    if best:
        out.append(" ".join(best))
    if len(tokens) > len(best):
        out.append(MASK)
    return " ".join(out)


def system_field(label: str, value, version=False, allowed=None) -> str:
    """One line of system.txt: the label, padded, and the value. A
    `version` field keeps its value only when the whole of it is a version,
    an `allowed` field only when it is one of those ("unknown" otherwise);
    any other field is a service state, cut by the Bridge row's phrases."""
    value = str(value)
    if version:
        shown = value if is_version(value) else "unknown"
    elif allowed is not None:
        shown = value if value in allowed else "unknown"
    else:
        shown = keep_phrase(value, STATUS_PHRASES["Bridge"])
    return f"{label:<11} {shown}"


def status_rows(text: str):
    """`blink status` output cut to the rows support reads: the label, then
    the row under keep_phrase. Returns (text, dropped).

    A line that does not open with a label is dropped, and that is every
    continuation line: they are the ones that carry a folder, another
    device, or a second number, and "version 42.17.93" on one of them once
    came through as a version."""
    kept, dropped = [], 0
    for line in text.splitlines():
        if not line.strip():
            continue
        label = next((l for l in STATUS_LABELS
                      if line == l or line.startswith(l + " ")), None)
        if label is None or "{" in line or "}" in line:
            dropped += 1
            continue
        rest = keep_phrase(line[len(label):], STATUS_PHRASES[label],
                           version_first=label in VERSION_ROWS)
        kept.append(f"{label:<11} {rest}".rstrip())
    return "".join(l + "\n" for l in kept), dropped


def redact_log_line(line: str):
    """One line of bridge.log as the bundle may hold it, or None to drop it.

    Kept: an optional logbook timestamp, one of LOG_TAGS, and the tag's
    phrase; or the constant BOARD_LINE. Dropped: a line that is not ours to
    vouch for (untagged output, a traceback, two writers glued together)
    and any line with a brace, which is a whole message from before
    2026-10-30 whatever tag it stands behind."""
    text = line.rstrip("\r\n")
    stamp = ""
    m = _STAMP.match(text)
    if m:
        stamp, text = m.group(1) + " ", text[m.end():]
    if text == BOARD_LINE:
        return stamp + text
    t = _TAG.match(text)
    if not t or t.group(1) not in LOG_TAGS or "{" in text or "}" in text:
        return None
    rest = keep_phrase(text[t.end():], PHRASES[t.group(1)])
    return (stamp + t.group(0) + " " + rest).rstrip()


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


def log_tail(blink_dir, cap_bytes=LOG_CAP_BYTES):
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
            out = redact_log_line(line)
            if out is None:
                dropped += 1
            else:
                kept.append(out)
    return kept, dropped, found


def _inside(path, home):
    home = os.path.normpath(home)
    p = os.path.normpath(path)
    return p == home or p.startswith(home + os.sep)


def _windows_desktop():
    """The Desktop folder Explorer shows, from the registry. With OneDrive
    backing up the Desktop it is ~\\OneDrive\\Desktop (or "OneDrive -
    <company>"), not ~\\Desktop, and a zip put in ~\\Desktop then is in a
    folder the owner never opens."""
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer"
                r"\User Shell Folders") as key:
            value, _ = winreg.QueryValueEx(key, "Desktop")
        return os.path.expandvars(value)
    except Exception:
        return None


def _xdg_desktop(home):
    """XDG_DESKTOP_DIR from ~/.config/user-dirs.dirs: a Linux desktop in
    another language names the folder for it ("Schreibtisch", "Bureau").
    Normalised, because the file writes "/" whatever the home path uses."""
    try:
        with open(os.path.join(home, ".config", "user-dirs.dirs"),
                  encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.match(r'\s*XDG_DESKTOP_DIR\s*=\s*"([^"]*)"', line)
                if m:
                    value = m.group(1)
                    if value.startswith("$HOME"):
                        value = home + value[len("$HOME"):]
                    return os.path.normpath(value)
    except OSError:
        pass
    return None


def desktop_dir(home):
    """The owner's Desktop folder, or None if there is none to be found.

    Only a folder inside `home` is taken. The registry and user-dirs.dirs
    answer for the real account whatever HOME says, and a Desktop outside
    the home being looked at is not one this run was asked about (under
    the test sandbox it would be the real desk's Desktop)."""
    candidates = []
    if sys.platform == "win32":
        candidates.append(_windows_desktop())
    elif not sys.platform.startswith("darwin"):
        candidates.append(_xdg_desktop(home))
    candidates.append(os.path.join(home, "Desktop"))
    if sys.platform == "win32":
        try:
            names = sorted(n for n in os.listdir(home)
                           if n.startswith("OneDrive"))
        except OSError:
            names = []
        candidates += [os.path.join(home, n, "Desktop") for n in names]
    for c in candidates:
        if c and _inside(c, home) and os.path.isdir(c):
            return c
    return None


def show_path(path, home):
    """`path` with the home folder shown as ~, for what the owner reads."""
    home = os.path.normpath(home)
    p = os.path.normpath(path)
    if _inside(p, home):
        return "~" + p[len(home):]
    return p


MANIFEST = """\
Blink support bundle, made by `blink support`.

  status.txt   the rows of `blink status` support reads
  system.txt   the app's and the board's versions, the operating system
               and the service state
  bridge.log   the newest part of the app's log

Every line was cut down before it was written here. A log line keeps its
time, its tag and the fixed phrase it opens with, from a short list; a
status row keeps its label and a phrase from another. Everything after
the phrase -- a name, a folder, a number, an id -- is shown as {mask},
but for two version numbers in status.txt. Lines that are not the app's
own are left out ({dropped} of them this time).
This is a filter, not a proof: read the files before you send them.
"""


def build_bundle(out_dir, status_text, system_text, log_lines, dropped=0,
                 today=None):
    """Write the zip and return its path.

    Named for the day, with -2, -3 ... after it rather than over an earlier
    one: an owner who ran it twice may already have sent the first, or be
    looking at it. Created with O_EXCL so a race cannot overwrite either.
    A write that fails takes its half-written file away with it, so the
    owner is never left holding a zip that will not open.
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
        break
    try:
        with os.fdopen(fd, "wb") as f, \
                zipfile.ZipFile(f, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("MANIFEST.txt", MANIFEST.format(
                mask=MASK, dropped=dropped))
            z.writestr("status.txt", status_text)
            z.writestr("system.txt", system_text)
            z.writestr("bridge.log", "".join(l + "\n" for l in log_lines))
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path
