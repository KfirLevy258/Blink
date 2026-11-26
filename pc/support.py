"""`blink support`: one zip a non-technical owner can send us (issue #20).

The Support section used to ask the owner to find ~/.blink/bridge.log and
paste `blink status` by hand, which is where a support exchange usually
stalled. This collects both, plus the versions, into one file on the Desktop.

What goes in is cut by an allowlist, not by a list of things to remove.
bridge.log is written through redacting paths today, but until 2026-10-30
whole messages, ids and usage figures were printed into it, and a log
rotated before that date can still be on a customer's disk. Removing the
shapes we can think of (UUIDs, hex, braces) passes the ones we did not: an
SSID printed bare, a session name, "session 42%".

The rule is by position. A log line keeps its timestamp, its tag and the
one fixed phrase it opens with, from that tag's list (PHRASES); whatever
follows is a single <x>, and a line that opens with no phrase is its tag
and <x>. A status row is its label and a phrase from STATUS_PHRASES the
same way. The phrase is written as it is typed below, never the line's own
text, so every word in a bundle is one a person put in this file. Keeping a
listed word wherever it stood was tried and let a value through when the
value was itself a listed word: a board whose id is "running", a network
called "network". A value never opens one of our lines. What is left over:
a line whose first words were filled in and spell a whole listed phrase
shows that phrase.

The board's console lines are the one exception. logbook.console_line logs
a known message as its constant text ("[ota] update REVERTED",
"rst:0xf (BROWNOUT_RESET)"), so a line that is exactly one of those
constants is kept whole: it says why a board restarted or whether an update
landed. With anything after the constant it is from an older log and is cut
like any other, which is why "[wifi] joined network" shows no network.

No number is kept from the log or from `blink status`: a usage figure can
be shaped like a version, and "version 42.17.93" on a continuation row once
came through as one. The versions are in system.txt, each asked for by the
command itself. Nothing here opens a serial port, touches the service, logs
what it read, or resolves home at import. Standard library only.
"""
import os
import re
import sys
import time
import zipfile

from pc import logbook
from pc.protocol import UNKNOWN_TYPE, WIRE_TYPES

# Per tag, the phrases a log line may open with: each the fixed opening of a
# line, ending before anything that is filled in. The keys are every tag our
# lines open with, and a line under any other tag is dropped, so
# tests/pc/test_support_cli.py derives the keys again from the source and
# fails when a tag was added and not listed here. Only the keys are held to
# the source. The phrases are a person's choice of what support reads a log
# for, and a changed log line fails nothing; an earlier shape pasted all 415
# line templates here and every change to a log line failed a test.
PHRASES = dict.fromkeys(
    "ask bl boot burn cfg claude desktop fatal focus logo mem panel plan proto "
    "settings sleep slide swipe tap test time touch usage watchdog".split(),
    ())
PHRASES.update({
    "bridge": (
        "NOT SENT:", "another Blink daemon is already running",
        "board found at", "connected on", "link ok:", "log rotated;",
        "main source:", "no usage data yet", "nothing here answers;",
        "ota: FAILED:", "ota: app update failed;", "ota: board has",
        "ota: closing port to flash", "ota: download failed:",
        "ota: flashed", "ota: flashing", "ota: not offering",
        "ota: offering", "ota: sent; waiting for the board to restart",
        "ota: updating this app to", "serial lost:",
        "the other daemon exited; taking over", "theme not sent:",
        "waiting for the board")
    # "[bridge] -> usage": the direction and type of each message, the
    # fixed names protocol.log_name writes.
    + tuple(way + " " + t for way in ("->", "<-")
            for t in sorted(WIRE_TYPES | {UNKNOWN_TYPE})),
    "ingest": ("Desktop data folder:", "Desktop folder lookup failed"),
    "log": ("last message repeated",),
    "ota": ("could not read the release feed:",),
    "spend": ("count not saved", "reading failed"),
    "status": ("check failed:",),
    "theme": ("builder could not start:", "builder ended:"),
    "update": (
        "check failed:", "could not read the release feed:",
        "could not start the new program:", "download failed:",
        "installing", "manifest signature did not verify",
        "restarting into the new version",
        "restored the previous program at"),
    "wifi": ("link lost", "the board joined but did not answer over WiFi",
             "the board was set up from another computer"),
    "wsl": ("check failed:",
            "the WSL distribution Blink was set up for is gone;"),
})

# The rows of `blink status` (pc/cli.py cmd_status) that are kept, by label,
# with the phrases each may open with. Any other row is left out: App and
# its version are in system.txt, and the Wire row is the usage message
# itself. "Codex hook" stands before "Codex" because the first match wins.
# system.txt's Service line is cut by the Bridge phrases.
STATUS_PHRASES = {
    "Bridge": ("running", "not running", "not installed", "not checked",
               "installed but not loaded", "registered but FAILING",
               "registered with launchd", "registered as a Scheduled Task",
               "registered to start when you log in"),
    "Link": ("USB cable", "no board connected", "WiFi, board at",
             "unknown (app not reporting)", "WiFi set up, board not found"),
    "Board": ("not plugged in", "none identified yet"),
    "Claude Code": ("not found", "on PATH, but", "WSL"),
    "Status line": ("installed at", "installed inside", "not installed"),
    "Activity": ("not checked", "hooks installed", "hooks not installed",
                 "PARTIAL"),
    "Usage data": ("fresh", "none yet", "from Claude Desktop's own files",
                   "from the Codex session log"),
    "Usage refresh": ("off", "off: Claude Code's /usage used the model",
                      "off: /usage kept failing;", "not set up", "on",
                      "on, but", "on, last ran"),
    "Session": ("no five-hour window",),
    "Desktop": ("usage cache parsed, reading", "no usage cache"),
    "Codex hook": ("not installed", "firing,"),
    "Codex": ("session log parsed, reading", "no session logs"),
}

# The newest 256 KB, about ten hours of a busy desk at the 24 KB/hour
# logbook.CAP_BYTES measured. The whole history is too big to email.
LOG_CAP_BYTES = 256 * 1024
LOG_FILES = ("bridge.log", "bridge.log.1")    # newest first
# Every board console line logbook.console_line writes whole.
CONSOLE_LINES = frozenset(
    logbook.CONSOLE_MESSAGES + logbook.CONSOLE_RENDERED
    + logbook.CONSOLE_EXTERNAL + (logbook.BOARD_LINE,))
MASK = "<x>"
SYSTEMS = ("Darwin", "Linux", "Windows")
MACHINES = ("arm64", "x86_64", "AMD64", "ARM64", "aarch64", "x86", "i686")

_STAMP = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d ")
_TAG = re.compile(r"^\[([a-z_-]+)\](?=\s|$)")
# Three or four numeric parts: "1.5" is as likely a figure as a version.
_VERSION = re.compile(r"^\d{1,4}(?:\.\d{1,4}){2,3}$")


def _bare(phrase):
    """A phrase's words without the punctuation they end in, lower case."""
    return [t.rstrip(",.:;") for t in phrase.lower().split()]


def keep_phrase(text, phrases) -> str:
    """The longest of `phrases` that `text` opens with, written as listed,
    then one mask if anything follows. Words are compared whole, punctuation
    included, without case: "usage=7" is not "usage", "off:" is not "off".

    A phrase that another one carries on from ("on", with "on, but" and
    "on, last ran" listed beside it) is kept only as the whole text. Where
    such a phrase ends, the next words could be filled in, and they would
    stand one comma away from spelling the longer phrase; so "on" followed
    by anything is the mask alone."""
    tokens = text.split()
    low = [t.lower() for t in tokens]
    best = []
    for phrase in phrases:
        want = phrase.lower().split()
        if len(want) > len(best) and low[:len(want)] == want:
            best = phrase.split()
    if best and len(tokens) > len(best):
        stem = _bare(" ".join(best))
        if any(len(b) > len(stem) and b[:len(stem)] == stem
               for b in map(_bare, phrases)):
            best = []
    return " ".join(best + [MASK] * (len(tokens) > len(best)))


def system_field(label, value, allowed=None) -> str:
    """One line of system.txt. The value is kept only when it is one of
    `allowed`, or, with no list, when the whole of it is a version."""
    value = str(value)
    ok = value in allowed if allowed else _VERSION.match(value)
    return f"{label:<11} {value if ok else 'unknown'}"


def status_rows(text):
    """`blink status` output cut to (kept text, lines dropped).

    A line that does not open with a known label is dropped, and that is
    every continuation line: they carry a folder, another device or a
    second number. The lines are not trusted to be rows our code printed
    (a device description can hold a line break), so each shows a label
    and a listed phrase and nothing else."""
    kept, dropped = [], 0
    for line in filter(str.strip, text.splitlines()):
        label = next((l for l in STATUS_PHRASES
                      if line == l or line.startswith(l + " ")), None)
        if label is None:
            dropped += 1
            continue
        rest = keep_phrase(line[len(label):], STATUS_PHRASES[label])
        kept.append(f"{label:<11} {rest}".rstrip() + "\n")
    return "".join(kept), dropped


def redact_log_line(line):
    """One line of bridge.log as the bundle may hold it, or None to drop it.

    Dropped: a line that is not ours to vouch for (untagged output, a
    traceback, a tag nobody listed) and any line with a brace, which is a
    whole message from before 2026-10-30 whatever tag it stands behind."""
    text = line.rstrip("\r\n")
    m = _STAMP.match(text)
    stamp, text = (m.group(0), text[m.end():]) if m else ("", text)
    if text in CONSOLE_LINES:
        return stamp + text
    t = _TAG.match(text)
    if not t or t.group(1) not in PHRASES or "{" in text or "}" in text:
        return None
    rest = keep_phrase(text[t.end():], PHRASES[t.group(1)])
    return (stamp + t.group(0) + " " + rest).rstrip()


def log_tail(blink_dir, cap_bytes=LOG_CAP_BYTES):
    """(lines, dropped, found): the redacted tail of bridge.log and the
    generation before it, oldest first; how many lines were dropped; and
    whether any log was there. The newest file has the budget first."""
    texts, budget, found = [], cap_bytes, False
    for name in LOG_FILES:
        try:
            with open(os.path.join(blink_dir, name), "rb") as f:
                data = f.read()
        except OSError:
            continue
        found = True
        if len(data) > budget:
            # From a line boundary, so no line starts half-way through.
            data = data[len(data) - budget:].partition(b"\n")[2]
        budget -= len(data)
        texts.insert(0, data.decode("utf-8", errors="replace"))
        if budget <= 0:
            break
    lines = [redact_log_line(l) for t in texts for l in t.splitlines()
             if l.strip()]
    kept = [l for l in lines if l is not None]
    return kept, len(lines) - len(kept), found


def _windows_desktop():
    """The Desktop Explorer shows, from the registry: with OneDrive backing
    it up it is ~\\OneDrive\\Desktop, and ~\\Desktop is a folder the owner
    never opens."""
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer"
                r"\User Shell Folders") as key:
            return os.path.expandvars(winreg.QueryValueEx(key, "Desktop")[0])
    except Exception:
        return None


def _xdg_desktop(home):
    """XDG_DESKTOP_DIR from ~/.config/user-dirs.dirs: a Linux desktop in
    another language names the folder for it ("Schreibtisch"). Normalised,
    because the file writes "/" whatever the home path uses."""
    try:
        with open(os.path.join(home, ".config", "user-dirs.dirs"),
                  encoding="utf-8", errors="replace") as f:
            m = re.search(r'^\s*XDG_DESKTOP_DIR\s*=\s*"([^"]*)"', f.read(),
                          re.M)
    except OSError:
        return None
    return os.path.normpath(m.group(1).replace("$HOME", home, 1)) if m \
        else None


def desktop_dir(home):
    """The owner's Desktop folder, or None. Only a folder inside `home` is
    taken: the registry answers for the real account whatever HOME says
    (under the test sandbox that is the real desk's Desktop)."""
    found = (_windows_desktop() if sys.platform == "win32"
             else None if sys.platform == "darwin" else _xdg_desktop(home))
    for c in (found, os.path.join(home, "Desktop")):
        if c and show_path(c, home) != os.path.normpath(c) \
                and os.path.isdir(c):
            return c
    return None


def show_path(path, home):
    """`path` with the home folder shown as ~, for what the owner reads."""
    home, p = os.path.normpath(home), os.path.normpath(path)
    if p == home or p.startswith(home + os.sep):
        return "~" + p[len(home):]
    return p


MANIFEST = """\
Blink support bundle, made by `blink support`.

  status.txt   the rows of `blink status` support reads
  system.txt   the versions, the operating system and the service state
  bridge.log   the newest part of the app's log

Every line was cut down before it was written here: it keeps its time,
its tag or label and the fixed phrase it opens with, from a short list.
Everything after the phrase (a name, a folder, a number, an id) is shown
as {mask}. Lines that are not the app's own are left out
({dropped} of them this time). This is a filter, not a proof: read the
files before you send them.
"""


def build_bundle(out_dir, status_text, system_text, log_lines, dropped=0,
                 today=None):
    """Write the zip and return its path.

    Named for the day, with -2, -3 ... rather than over an earlier one: the
    owner may already have sent the first. O_EXCL (through os.open, since a
    builtin open in "xb" trips tests/test_text_encoding.py) so a race cannot
    overwrite either. A write that fails takes its half-written file with
    it, so nobody is left holding a zip that will not open."""
    os.makedirs(out_dir, exist_ok=True)
    stem = "blink-support-" + (today or time.strftime("%Y-%m-%d"))
    n = 1
    while True:
        path = os.path.join(out_dir, stem + ("" if n == 1 else f"-{n}")
                            + ".zip")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | getattr(os, "O_BINARY", 0), 0o644)
            break
        except FileExistsError:
            n += 1
    try:
        with os.fdopen(fd, "wb") as f, \
                zipfile.ZipFile(f, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("MANIFEST.txt",
                       MANIFEST.format(mask=MASK, dropped=dropped))
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
