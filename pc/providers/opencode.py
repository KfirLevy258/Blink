"""OpenCode on the GLM Coding Plan, as a third provider on the ingestion bus.

Two frames, under provider "opencode" (docs/plans/opencode.md is the design
and the measurements behind it):

  - a usage frame, src "zai", from the plan's own quota endpoint;
  - a state frame, src "state", from the hook slots tools/blink-opencode.js
    writes into ~/.blink/state-opencode in exactly blink-hook.sh's format,
    read by claude_state -- its state machine, waiting marker, tombstone and
    sweep, unchanged, as codex_state already does. Pid liveness is the one
    piece that is OpenCode's own: "trusted once seen alive" (SEEN_ALIVE
    below), never the process-wide suspension Claude and Codex share.

WHY A NETWORK REQUEST AT ALL
----------------------------
OpenCode stores token counts and cost in opencode.db and no limit, so there
is no percentage anywhere on disk. The plan's endpoint has one:

    GET https://api.z.ai/api/monitor/usage/quota/limit
    Authorization: <the Coding Plan API key, raw, no "Bearer">

which is what Z.ai's own usage plugin sends (zai-coding-plugins,
query-usage.mjs). This is the first time the daemon makes a request with a
customer's credential, so the rules are strict and each one is enforced in
one place below:

  - The key is read from OpenCode's auth.json at each fetch (_read_key) and
    held only in the request header. auth.json is opened only once a fetch
    is due, never on the ticks in between (tick). quota_source() -- the
    thing `blink status` and the cadence look at -- never returns it.
  - It goes only to the host it was issued for: z.ai for zai-coding-plan,
    BigModel for zhipuai-coding-plan. Redirects are refused outright, because
    urllib copies every header but the content ones onto a redirect -- a 302
    from either host would otherwise hand the key to wherever it pointed.
  - Nothing about a fetch is ever logged except a short category
    ("unreachable", "rejected the key", "unexpected reply", "certificate not
    trusted", "no plan key"), once per change. Not the exception text -- an
    HTTPError's message, a proxy's error page or a future library's repr can
    all carry a header -- and never the body.

OBSERVED_AT, ACTIVE_AT, AND THE SECOND RING
-------------------------------------------
Two requirements pull these apart, and this provider is where they first
disagree:

  1. A reading fetched a minute ago is a minute old. The endpoint is the
     account's own ledger, including use from other machines, so the number
     is as fresh as the fetch, whatever OpenCode on THIS desk has been doing.
     If observed_at were the last local activity, a correct, just-read figure
     would arrive with age_s of three hours and go `stale` -- "Reading is
     old" over a number that is not.
  2. On a desk running Claude, Codex and OpenCode, the second ring belongs to
     the tool USED most recently. If observed_at were the fetch time and the
     ring were chosen on it, OpenCode would take the ring from Codex every ten
     minutes just by being polled.

So observed_at is the FETCH (the age of the number: requirement 1, and
`stale` is measured from it), and active_at is the last OpenCode ACTIVITY --
the newest mtime of the DB, its WAL and the hook slots (requirement 2). The
state frame follows the same rule: its active_at is its newest slot's clock,
or now while a session is running (claude_state.activity_at).
pc/normalizer.select_pair orders on active_at; for every other provider that
is the same value it always sorted on, because active_at defaults to
observed_at. active_at also decides when the board dozes (protocol
`active_age_s`), and a fetch every ten minutes is not somebody at the desk:
with the fetch time there the panel would never sleep on a machine where
OpenCode was opened this week.

This is the one frame on the bus whose active_at can be OLDER than its
observed_at. base.NormalizedUsageFrame describes active_at as the freshest
contact and was written when every reading was a contact; a fetch is not one.

WHEN TO FETCH
-------------
On a background thread, so poll() -- every two seconds on the daemon's fast
poll -- never waits on the network:

  - at most once a minute while OpenCode was active in the last ten minutes;
  - every ten minutes otherwise;
  - not at all once it has been idle for seven days, or with no plan key.

A failure keeps the last good reading, which then ages into `stale` like any
other -- except a failure that is about the key: the plan refusing it, or
the key gone from auth.json. Those drop the reading, because the number
then describes an account this desk can no longer see. So does reaching the
seven-day give-up: nothing would ever replace that reading. The thread starts
on the first poll(), not in the constructor, so building a provider --
`blink status`, a test, default_providers() -- spawns nothing until somebody
actually polls one with fetch=True.

NOT HANDLED, deliberately
-------------------------
  - OPENCODE_DB and OPENCODE_AUTH_CONTENT. Both are environment variables of
    the shell OpenCode runs in, and the daemon runs as a login service with
    an environment of its own: honouring them here would read some other
    variable's value, or none, and look like it worked.
  - OpenCode 2.0's `credential` table. Its dev branch moves auth.json into
    SQLite; tests/ci/check_opencode_contract.sh is what fails that day.
  - Pay-as-you-go keys (provider `zai`): they have no plan quota to show.
"""
import collections
import glob
import json
import math
import os
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request

from pc.providers import base
from pc.providers import claude_state

PROVIDER_ID = "opencode"
SRC_ID = "zai"
STATE_SRC_ID = "state"

# Expanded when a provider is built, never here: a module-level expanduser is
# evaluated at import, before tests/conftest.py can move HOME, and the scan
# behind it DELETES abandoned slots. Same rule as claude_state.STATE_DIR.
STATE_DIR = "~/.blink/state-opencode"

# Which of this directory's pids this daemon process has seen alive, for
# claude_state.SeenAlivePids -- OpenCode's own pid rule, kept apart from the
# process-wide suspension Claude and Codex share.
#
# Found end to end with real OpenCode 1.19.3, 2026-10-19: a TUI quit with
# ctrl-c, or `serve` sent SIGTERM, delivers no 'exit' and so never writes
# SessionEnd, and quitting within FRESH_SLOT_S of the last event left the
# fresh-slot-dead-pid that the global flag reads as "pids mean nothing on
# this machine". The flag went False; the closed session held an amber "your
# turn" for an hour, and Claude's and Codex's dead-pid drops went off with
# it. Here a pid seen alive at any poll ends its session the moment it is
# gone, whatever the slot's age, and a pid never seen alive (a container's,
# under a bind-mounted ~/.blink) is never used to end one.
#
# Module-level rather than per provider instance, because what this process
# has watched is a fact about the process: a provider rebuilt mid-run must
# not forget that it saw OpenCode alive a poll ago.
SEEN_ALIVE = claude_state.SeenAlivePids()


def reset_pid_memory():
    """Forget every pid seen alive. For tests: the memory is process-wide
    and would otherwise carry one test's live pid into the next."""
    SEEN_ALIVE.reset()


# The two plan providers OpenCode knows, in the order they are tried, and the
# only host each key is ever sent to. A global key on the mainland host (or
# the reverse) is refused there, so the id is the routing as well as the key.
QUOTA_HOSTS = (
    ("zai-coding-plan", "https://api.z.ai"),
    ("zhipuai-coding-plan", "https://open.bigmodel.cn"),
)
QUOTA_PATH = "/api/monitor/usage/quota/limit"

# A fetch that has not answered in this long is not going to. Ten seconds is
# far past the endpoint's ordinary reply and short enough that a dead network
# never leaves the thread stuck past its next due time.
FETCH_TIMEOUT_S = 10.0

# `blink status` asks once, in front of a person waiting for its output, and
# its every other line works offline -- on a plane, the full ten seconds
# would be the slowest part of the command, spent to print "unreachable".
# The daemon's thread can afford to wait; the person cannot.
STATUS_FETCH_TIMEOUT_S = 4.0

# The reply is a few hundred bytes. Anything much larger is not the quota
# endpoint (a captive portal, a proxy's error page) and is not read whole.
MAX_REPLY_BYTES = 64 * 1024

# auth.json holds a handful of keys. A file this large is not one.
MAX_AUTH_BYTES = 1024 * 1024

# The cadence. See WHEN TO FETCH above.
ACTIVE_WINDOW_S = 600.0
ACTIVE_INTERVAL_S = 60.0
IDLE_INTERVAL_S = 600.0
GIVE_UP_AFTER_S = 7 * 86400.0

# How often the thread wakes to ask whether a fetch is due. A few stat calls;
# five seconds keeps "within a minute of activity" honest without a busy loop.
TICK_S = 5.0

# Same bound as the other usage sources (codex_cli.STALE_AFTER_S). With the
# ten-minute idle cadence a working fetch never reaches it; three failed
# fetches in a row do.
STALE_AFTER_S = 1800

# The windows, by the endpoint's own (unit, number), never by position or
# type: both entries can carry the same type. Unit 3 is hours and unit 6 is
# weeks in Z.ai's enum; unit 5 number 1 is the legacy monthly tool quota
# (TIME_LIMIT), which is not a window the panel draws.
SESSION_WINDOW = (3, 5)
WEEKLY_WINDOW = (6, 1)
# CREDIT_LIMIT since the plan relaunch of 2026-07-30; TOKENS_LIMIT on plans
# bought before it, which still report until they expire.
LIMIT_TYPES = ("TOKENS_LIMIT", "CREDIT_LIMIT")

# A 5-hour reset further out than this is the server bug CodexBar #2712 /
# #2871 describe: the stamp is sometimes about ten hours ahead. A countdown
# that says nine hours on a five-hour window is the confident wrong number
# this project refuses; no countdown is the honest one. Sixty seconds of slack
# for the two clocks disagreeing.
SESSION_RESET_SLACK_S = 5 * 3600.0 + 60.0

# How far behind this desk's clock a reset may sit and still count as not yet
# passed. Third review, 2026-10-19: the test was a bare `reset <= now`, so a
# desk whose clock runs a couple of seconds ahead of Z.ai's -- ordinary NTP
# drift -- read a window rolling over on the server's next tick as already
# rolled, and blanked a reading fetched that same second, for as long as
# the next fetch took (a minute while OpenCode is in use, ten otherwise). A
# window the server still reports IS still running; the desk's clock is the
# one that is early. Five seconds is skew, not a cached reply: a reply held
# anywhere for longer than that is the stale one the strict test was for,
# and it still blanks. The same slack retires a kept window in _usage_frame,
# so a reading parsed inside it is not dropped by the very next poll.
RESET_CLOCK_SLACK_S = 5.0

# Plausible bounds for a reset, in seconds: 2020-01-01 to 2100-01-01, as the
# other sources bound theirs. What it notices is a unit change.
RESET_EPOCH_MIN = 1_577_836_800
RESET_EPOCH_MAX = 4_102_444_800

# The endpoint answers HTTP 200 for a failure and says so in the body. These
# codes mean the key itself: 1000/1001/1002 are Z.ai's authentication family
# (1001 is "no key", measured), 401 is a bad key (measured).
AUTH_FAILURE_CODES = (401, 1000, 1001, 1002)

# The only words a fetch is ever reported in. See the module docstring.
ERR_UNREACHABLE = "unreachable"
ERR_REJECTED = "rejected the key"
ERR_UNEXPECTED = "unexpected reply"
ERR_NO_KEY = "no plan key"
ERR_CERT = "certificate not trusted"

Quota = collections.namedtuple(
    "Quota",
    ("session_pct", "session_resets_at", "weekly_pct", "weekly_resets_at"))


# --- where OpenCode keeps things ---------------------------------------------


# Where `blink install` records OpenCode's directories when the shell it ran
# in resolved them through XDG variables to somewhere other than the
# defaults. Expanded late, like STATE_DIR.
#
# Found in review, 2026-10-19. The shell a person installs from and the
# service the daemon runs as do not share an environment: the launchd plist,
# the systemd user unit and the Windows scheduled task carry none of the
# XDG_* variables, and could not be made to without each platform's service
# definition learning about one tool's paths. So on a desk with
# XDG_DATA_HOME=/data, `blink install` and `blink status` found OpenCode in
# /data/opencode, installed the plugin into $XDG_CONFIG_HOME, and reported
# everything working -- while the daemon looked in ~/.local/share/opencode,
# found no database and no key, and the OpenCode gauges never appeared. The
# record is how what the shell saw reaches the service, on every platform,
# without depending on the service's environment at all.
PATHS_RECORD = "~/.blink/opencode-paths.json"

# The record is a few hundred bytes; a larger file is not one we wrote.
MAX_RECORD_BYTES = 64 * 1024


def _home_path(spec):
    """expanduser, then normpath, so a path reads one way on every platform.

    expanduser("~/.blink/x") on Windows is C:\\Users\\me/.blink/x -- the
    profile with backslashes, the rest with the forward slashes written here.
    Windows opens either, but install prints this path to the customer
    ("Creates  ..."), the plugin computes the same directory with node's
    path.join, which says C:\\Users\\me\\.blink\\state-opencode, and the
    tests compare against os.path.join; the half-and-half form matched none
    of them, and the Windows job failed two tests on it (CI, 2026-10-19).
    normpath is the identity for these paths on macOS and Linux.
    """
    return os.path.normpath(os.path.expanduser(spec))


def paths_record_path():
    return _home_path(PATHS_RECORD)


def _default_dir(*parts):
    return os.path.join(os.path.expanduser("~"), *parts, "opencode")


def _env_dir(var):
    """$VAR/opencode, or None when VAR is unset or empty -- an empty
    variable is unset, as xdg-basedir's `||` treats it."""
    xdg = os.environ.get(var)
    return os.path.join(xdg, "opencode") if xdg else None


def recorded_dirs():
    """{"data_dir": ..., "config_dir": ...} from the record, keys absent
    when not recorded. Never raises: an unreadable, malformed or foreign
    record is no record, and the defaults apply."""
    try:
        with open(paths_record_path(), "rb") as f:
            raw = f.read(MAX_RECORD_BYTES + 1)
    except OSError:
        return {}
    if len(raw) > MAX_RECORD_BYTES:
        return {}
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return {}
    if not isinstance(doc, dict):
        return {}
    # Absolute paths only. A relative one would resolve against whatever
    # directory the service happened to start in.
    return {k: doc[k] for k in ("data_dir", "config_dir")
            if isinstance(doc.get(k), str) and os.path.isabs(doc[k])}


def data_dir():
    """$XDG_DATA_HOME/opencode, else what install recorded, else
    ~/.local/share/opencode.

    On macOS too, not ~/Library, and on Windows under %USERPROFILE%: OpenCode
    takes its paths from xdg-basedir 5.1.0, which has no per-platform case,
    and expanduser("~") is USERPROFILE there just as os.homedir() is in node.

    The variable first, because in a shell that sets it that IS where
    OpenCode is. The record second, because the service has no such
    variable and the record is what the shell saw (PATHS_RECORD).
    """
    return (_env_dir("XDG_DATA_HOME") or recorded_dirs().get("data_dir")
            or _default_dir(".local", "share"))


def config_dir():
    """$XDG_CONFIG_HOME/opencode, else the record, else ~/.config/opencode.
    Same rules."""
    return (_env_dir("XDG_CONFIG_HOME") or recorded_dirs().get("config_dir")
            or _default_dir(".config"))


def service_dirs():
    """(data_dir, config_dir) as the SERVICE resolves them: the record, else
    the defaults, with no environment at all. What `blink status` compares
    the shell's answer against."""
    rec = recorded_dirs()
    return (rec.get("data_dir") or _default_dir(".local", "share"),
            rec.get("config_dir") or _default_dir(".config"))


def record_dirs():
    """Make the record match this shell's XDG view. For `blink install`.

    Written only when the variables move a directory off its default, and
    removed when they do not: a record left from an install under a
    different environment would point the service at a directory this
    shell no longer uses. Atomic (temp file, then os.replace) and 0600 --
    it holds paths, not secrets, but paths under someone's home are theirs.
    Returns a short line for the install output, or None when there was
    nothing to record and nothing to remove.
    """
    want = {}
    d, c = _env_dir("XDG_DATA_HOME"), _env_dir("XDG_CONFIG_HOME")
    if d and os.path.abspath(d) != _default_dir(".local", "share"):
        want["data_dir"] = os.path.abspath(d)
    if c and os.path.abspath(c) != _default_dir(".config"):
        want["config_dir"] = os.path.abspath(c)
    path = paths_record_path()
    if not want:
        return "removed the old OpenCode paths record" if forget_dirs() else None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(want, f, indent=1, sort_keys=True)
            f.write("\n")
        try:
            os.chmod(tmp, 0o600)     # a stale temp keeps its old mode
        except OSError:
            pass
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return f"recorded OpenCode's directories for the service in {path}"


def forget_dirs():
    """Remove the record. True when there was one. For uninstall."""
    try:
        os.remove(paths_record_path())
        return True
    except OSError:
        return False


def state_dir():
    """Where the plugin writes its slots, expanded now rather than at import."""
    return _home_path(STATE_DIR)


def db_paths(root=None):
    """OpenCode's SQLite files in the data directory, opencode.db first.

    Other release channels write opencode-<channel>.db beside it. The WAL and
    shared-memory files are not matched -- the glob ends in `.db` -- and
    last_activity() adds the WAL back on purpose.
    """
    root = root if root is not None else data_dir()
    try:
        found = [p for p in glob.glob(os.path.join(glob.escape(root),
                                                   "opencode*.db"))
                 if os.path.isfile(p)]
    except OSError:
        return []
    return sorted(found, key=lambda p: (os.path.basename(p) != "opencode.db",
                                        os.path.basename(p)))


def present(root=None) -> bool:
    """Has OpenCode run on this machine?

    The database, not the binary, for the reason cli.codex_present gives for
    reading rollouts: the file is what proves the tool has been used here,
    and a binary on PATH proves only that somebody installed it. OpenCode
    creates opencode.db on its first start.
    """
    try:
        return bool(db_paths(root))
    except Exception:
        return False


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def last_activity(root=None, slots=None):
    """The newest sign of OpenCode being used, as an epoch, or None.

    The DB AND its WAL: OpenCode runs SQLite in WAL mode, and between
    checkpoints most writes land in opencode.db-wal while opencode.db itself
    does not move (measured on 1.19.3). Then the hook slots, whose files the
    plugin rewrites on every event -- including the tombstone and the waiting
    marker, and the agent files inside each session's directory.

    Not the slot directory's own mtime, although the design names it: the
    daemon's own sweep deletes files there, and a sweep is not somebody using
    OpenCode. Counting it would make one collection of an hour-old slot look
    like activity -- a minute-cadence fetch for ten minutes and the second
    ring for the rest of the hour.
    """
    root = root if root is not None else data_dir()
    slots = slots if slots is not None else state_dir()
    newest = None
    for db in db_paths(root):
        for p in (db, db + "-wal"):
            t = _mtime(p)
            if t is not None and (newest is None or t > newest):
                newest = t
    try:
        entries = os.listdir(slots)
    except OSError:
        entries = []
    for name in entries:
        p = os.path.join(slots, name)
        if os.path.isdir(p):
            try:
                inner = [os.path.join(p, n) for n in os.listdir(p)]
            except OSError:
                inner = []
        else:
            inner = [p]
        for q in inner:
            t = _mtime(q)
            if t is not None and (newest is None or t > newest):
                newest = t
    return newest


# --- the key -----------------------------------------------------------------


def _load_auth(root=None):
    """auth.json as a dict, or None. Never raises, never echoes content."""
    root = root if root is not None else data_dir()
    path = os.path.join(root, "auth.json")
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_AUTH_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_AUTH_BYTES:
        return None
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return doc if isinstance(doc, dict) else None


def _entry_key(doc, provider_key):
    """The key one auth.json entry holds, or None when it is not a plan key.

    `type: "api"` only. OpenCode also writes `oauth` and `wellknown` entries,
    and neither is a Coding Plan API key the quota endpoint would accept.
    """
    entry = doc.get(provider_key)
    if not isinstance(entry, dict) or entry.get("type") != "api":
        return None
    key = entry.get("key")
    if not isinstance(key, str):
        return None
    key = key.strip()
    # A header value cannot carry a line break, and a key that has one is not
    # a key -- refusing it here keeps http.client's own error message (which
    # quotes the value) from ever being raised with it inside.
    if not key or any(c in key for c in "\r\n\0"):
        return None
    return key


def quota_source(root=None):
    """(provider_key, host) for the plan key OpenCode holds, or None.

    Deliberately NOT the key. This is what the cadence and `blink status`
    look at, and neither has any use for the secret -- so neither is handed
    one it could print.
    """
    doc = _load_auth(root)
    if doc is None:
        return None
    for provider_key, host in QUOTA_HOSTS:
        if _entry_key(doc, provider_key) is not None:
            return provider_key, host
    return None


def _read_key(provider_key, root=None):
    """The key itself, for the one request that needs it, or None."""
    doc = _load_auth(root)
    if doc is None:
        return None
    return _entry_key(doc, provider_key)


# --- the reply ---------------------------------------------------------------


def _number(v):
    """A finite int or float, or None. bool is an int in Python and is not."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if math.isfinite(v) else None


def _limit_pct(limit):
    """One limit's used percentage, clamped to 0..100, or None.

    `usage` is the ALLOWANCE, not what was used -- `currentValue` is the use.
    Their ratio is preferred because the server's `percentage` is a rounded
    integer; old TOKENS_LIMIT plans sometimes send `percentage` alone.
    """
    cur, allowance = _number(limit.get("currentValue")), _number(
        limit.get("usage"))
    if cur is not None and allowance is not None and allowance > 0:
        pct = cur / allowance * 100.0
    else:
        pct = _number(limit.get("percentage"))
        if pct is None:
            return None
    return min(100.0, max(0.0, pct))


def _limit_reset(limit, now_epoch, max_ahead_s=None):
    """nextResetTime in epoch seconds, or None when it cannot be believed.

    Milliseconds on the wire. A reset already past describes a window that
    has rolled, and a 5-hour reset more than five hours out is the server
    bug in SESSION_RESET_SLACK_S.
    """
    ms = _number(limit.get("nextResetTime"))
    if ms is None:
        return None
    s = ms / 1000.0
    if not (RESET_EPOCH_MIN <= s <= RESET_EPOCH_MAX):
        return None
    if s <= now_epoch - RESET_CLOCK_SLACK_S:
        return None
    if max_ahead_s is not None and s > now_epoch + max_ahead_s:
        return None
    return s


def _reset_has_passed(limit, now_epoch):
    """True when the limit names a plausible reset that is already behind
    now_epoch by more than RESET_CLOCK_SLACK_S. A missing or implausible
    stamp is not "passed": that window keeps its percentage with no
    countdown, as _limit_reset decides."""
    ms = _number(limit.get("nextResetTime"))
    if ms is None:
        return False
    s = ms / 1000.0
    return (RESET_EPOCH_MIN <= s <= RESET_EPOCH_MAX
            and s <= now_epoch - RESET_CLOCK_SLACK_S)


def parse_quota(payload, now_epoch):
    """A Quota from the endpoint's reply, or None. Never raises.

    The reply is HTTP 200 whether or not it worked, so `success` must be True
    and `code` 200 before anything else is read. The windows are matched by
    (unit, number) among TOKENS_LIMIT and CREDIT_LIMIT entries; the first
    entry with a percentage wins its window. None when neither window has
    one, because a frame with no numbers must not win a recency contest for
    numbers it does not have.
    """
    try:
        return _parse_quota(payload, now_epoch)
    except Exception:
        return None


def _parse_quota(payload, now_epoch):
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8")
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        return None
    if payload.get("success") is not True:
        return None
    code = payload.get("code")
    if isinstance(code, bool) or code not in (200, "200"):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    limits = data.get("limits")
    if not isinstance(limits, list):
        return None

    found = {}
    for limit in limits:
        if not isinstance(limit, dict):
            continue
        if limit.get("type") not in LIMIT_TYPES:
            continue
        unit, number = limit.get("unit"), limit.get("number")
        if isinstance(unit, bool) or isinstance(number, bool):
            continue
        window = (unit, number)
        if window not in (SESSION_WINDOW, WEEKLY_WINDOW) or window in found:
            continue
        pct = _limit_pct(limit)
        if pct is None:
            continue
        if _reset_has_passed(limit, now_epoch):
            # The window this percentage describes is already over at the
            # moment it is read: a reply cached somewhere on the way, or this
            # desk's clock ahead of the server's. Found in review,
            # 2026-10-19: the percentage was kept with its reset dropped to
            # None -- and _usage_frame expires a window only by its reset, so
            # a number for a window that had already rolled stayed on the
            # panel, unexpiring, until the next good fetch. Usage already
            # forgiven, shown with confidence. Treated as a window with no
            # percentage, so a later entry for it may still fill it.
            continue
        reset = _limit_reset(
            limit, now_epoch,
            SESSION_RESET_SLACK_S if window == SESSION_WINDOW else None)
        found[window] = (pct, reset)

    if not found:
        return None
    s_pct, s_reset = found.get(SESSION_WINDOW, (base.UNKNOWN, None))
    w_pct, w_reset = found.get(WEEKLY_WINDOW, (base.UNKNOWN, None))
    return Quota(s_pct, s_reset, w_pct, w_reset)


def describe(quota, age_s=None):
    """One line for `blink status`: "GLM plan quota: 34% (5h), 12% (week)".

    `age_s`, when given, adds ", read N s ago". Sentence case, and short,
    because it is read by a person scanning a status listing.
    """
    def pct(v):
        return f"{v:.0f}%" if v is not None and v >= 0 else "--%"
    text = (f"GLM plan quota: {pct(quota.session_pct)} (5h),"
            f" {pct(quota.weekly_pct)} (week)")
    if age_s is not None and age_s >= 0:
        text += f", read {int(age_s)} s ago"
    return text


# --- one fetch ---------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect, so the key cannot follow one off its host.

    urllib's own redirect_request copies the request's headers onto the new
    request, minus the content ones -- Authorization included -- so a 302 from
    the quota host would deliver the key to wherever it pointed. Returning
    None makes urllib raise the 3xx as an HTTPError, which _fetch reports as
    an unexpected reply.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _user_agent():
    try:
        from pc import version
        return f"blink/{version.RELEASE_VERSION}"
    except Exception:
        return "blink"


def _ssl_context():
    """pc/ota's system-plus-certifi context, for the reason it gives there:
    the frozen macOS build has no usable CA bundle of its own."""
    try:
        from pc import ota
        return ota._ssl_context()
    except Exception:
        return ssl.create_default_context()


def http_get(url, key, timeout=FETCH_TIMEOUT_S):
    """GET one quota URL with the key, returning the body's bytes.

    Raises whatever urllib raises. The caller (_fetch) catches everything and
    reports a category, never the exception -- an HTTPError carries the
    response headers, and nothing here vouches for what is in them.
    """
    req = urllib.request.Request(url, method="GET")
    # Unredirected as well as refused-on-redirect: two locks on one door,
    # because a future change to the opener should not be all it takes.
    req.add_unredirected_header("Authorization", key)
    req.add_header("Accept", "application/json")
    req.add_header("Accept-Language", "en-US,en")
    req.add_header("User-Agent", _user_agent())
    opener = urllib.request.build_opener(
        _NoRedirect, urllib.request.HTTPSHandler(context=_ssl_context()))
    with opener.open(req, timeout=timeout) as r:
        return r.read(MAX_REPLY_BYTES + 1)


def _category_for(exc):
    """The words a failed request is reported in. Reads the type and the
    status code only -- never str(exc)."""
    reason = getattr(exc, "reason", None)
    if isinstance(exc, ssl.SSLCertVerificationError) or isinstance(
            reason, ssl.SSLCertVerificationError):
        # Its own words, because "unreachable" is what hid exactly this on
        # every frozen macOS install for a day (see pc/ota._ssl_context).
        return ERR_CERT
    if isinstance(exc, urllib.error.HTTPError):
        code = getattr(exc, "code", None)
        # 401 only. A 403 comes from whatever stands in front of the
        # endpoint as often as from the endpoint -- a WAF, a geo block, a
        # corporate proxy, Cloudflare's challenge page -- and none of those
        # has looked at the key. Found in review, 2026-10-19: 403 was read as
        # "rejected the key", which also DROPS the reading (tick), so one
        # blocked request from a café network blanked a correct gauge and
        # told the person their key was bad. The plan's own refusals arrive
        # as HTTP 401 or in the body (AUTH_FAILURE_CODES), and only those
        # are about the key.
        if code == 401:
            return ERR_REJECTED
        if isinstance(code, int) and code >= 500:
            return ERR_UNREACHABLE
        return ERR_UNEXPECTED
    if isinstance(exc, (urllib.error.URLError, OSError, TimeoutError)):
        return ERR_UNREACHABLE
    return ERR_UNEXPECTED


def _fetch(source, now_epoch, fetcher=None, root=None,
           timeout=FETCH_TIMEOUT_S):
    """(Quota or None, error category or None) for one request. Never raises.

    The key exists in this frame only, for the length of one call to the
    fetcher, and every path out of here drops it.
    """
    provider_key, host = source
    fetcher = fetcher if fetcher is not None else http_get
    key = _read_key(provider_key, root)
    if key is None:
        return None, ERR_NO_KEY
    try:
        try:
            body = fetcher(host + QUOTA_PATH, key, timeout)
        finally:
            key = None
    except Exception as e:
        category = _category_for(e)
        try:
            # An HTTPError is also the open response; close it unread.
            close = getattr(e, "close", None)
            if close is not None:
                close()
        except Exception:
            pass
        return None, category

    try:
        if isinstance(body, (bytes, bytearray)):
            if len(body) > MAX_REPLY_BYTES:
                return None, ERR_UNEXPECTED
            doc = json.loads(bytes(body).decode("utf-8"))
        elif isinstance(body, str):
            doc = json.loads(body)
        else:
            doc = body
    except Exception:
        return None, ERR_UNEXPECTED
    if not isinstance(doc, dict):
        return None, ERR_UNEXPECTED
    if doc.get("success") is not True:
        code = doc.get("code")
        if isinstance(code, str) and code.isdigit():
            code = int(code)
        if not isinstance(code, bool) and code in AUTH_FAILURE_CODES:
            return None, ERR_REJECTED
        return None, ERR_UNEXPECTED
    quota = parse_quota(doc, now_epoch)
    if quota is None:
        return None, ERR_UNEXPECTED
    return quota, None


def fetch_once(now_epoch=None, fetcher=None, root=None,
               timeout=FETCH_TIMEOUT_S):
    """ONE synchronous fetch, for `blink status` when it is asked to look.

    (Quota or None, error category or None). (None, None) means there is no
    plan key to ask with, which is not an error: OpenCode on another model
    has no plan quota. Never raises and never prints.
    """
    now_epoch = time.time() if now_epoch is None else now_epoch
    try:
        source = quota_source(root)
        if source is None:
            return None, None
        return _fetch(source, now_epoch, fetcher, root, timeout)
    except Exception:
        return None, ERR_UNEXPECTED


# --- the provider ------------------------------------------------------------


class OpenCodeProvider(base.ProviderParser):
    """Usage from the plan endpoint, state from the plugin's slots.

    `fetch` decides whether poll() may start the background fetcher; False
    is for `blink status` and tests, which then call tick() themselves when
    they want a fetch. `fetcher(url, key, timeout) -> bytes` and `clock`
    replace the network and the wall clock in tests. `data_path` and
    `state_dir` replace OpenCode's data directory and the slot directory;
    `sweep` is forwarded to claude_state, so a caller that only wants to
    look can look without collecting. `liveness` replaces SEEN_ALIVE in
    tests.
    """

    def __init__(self, state_dir=None, data_path=None, sweep=True,
                 fetch=True, fetcher=None, clock=time.time, liveness=None):
        self._state_dir = (state_dir if state_dir is not None
                           # Not state_dir(): the parameter shadows it.
                           else _home_path(STATE_DIR))
        self._data_path = data_path
        self._state = claude_state.ClaudeStateProvider(
            path=self._state_dir, sweep=sweep,
            liveness=liveness if liveness is not None else SEEN_ALIVE)
        self._fetch_enabled = fetch
        self._fetcher = fetcher
        self._clock = clock
        self._lock = threading.Lock()
        self._reading = None        # (Quota, fetched_at)
        self._error = None          # the category of the last failure
        self._reported = None       # the category stderr last carried
        self._last_attempt = None
        self._thread = None
        self._stop = threading.Event()

    def get_provider_id(self) -> str:
        return PROVIDER_ID

    def state_path(self):
        return self._state_dir

    def _root(self):
        return self._data_path if self._data_path is not None else data_dir()

    # --- what status can ask without touching the network ------------------

    def last_reading(self):
        """(Quota, fetched_at) for the last good fetch, or None."""
        with self._lock:
            return self._reading

    def last_error(self):
        """The category of the most recent failed fetch, or None once one
        succeeds again."""
        with self._lock:
            return self._error

    def remember(self, quota, fetched_at):
        """Take a reading somebody else fetched, as if this provider had.

        For `blink status --wire`, which has already made its one request
        (fetch_once) for the OpenCode row and builds this provider with
        fetch=False: the wire line it prints should carry the figure the
        daemon would send, and fetching it a second time to get there would
        be a second request with the customer's key for the same answer.
        """
        if quota is None:
            return
        with self._lock:
            self._reading = (quota, fetched_at)
            self._error = None

    # --- the cadence --------------------------------------------------------

    def fetch_interval(self, now_epoch):
        """Seconds between fetches right now, or None for "do not fetch".

        The whole answer, key included, for `blink status` and the tests.
        tick() does NOT call this: it asks _cadence() first and reads
        auth.json only once a fetch is actually due -- see tick().
        """
        if quota_source(self._root()) is None:
            return None
        return self._cadence(now_epoch)

    def _cadence(self, now_epoch):
        """The interval from OpenCode's activity alone, or None for "do not
        fetch". A few stat calls; it never opens auth.json."""
        la = last_activity(self._root(), self._state_dir)
        if la is None:
            return None
        # A stamp in the future is a clock that stepped, not a sign of
        # nothing: count it as active, as derive_state counts a negative age
        # as fresh.
        idle = max(0.0, now_epoch - la)
        if idle > GIVE_UP_AFTER_S:
            return None
        return ACTIVE_INTERVAL_S if idle <= ACTIVE_WINDOW_S else IDLE_INTERVAL_S

    def tick(self, now_epoch):
        """Fetch if one is due. True when a fetch was attempted.

        Synchronous: the thread calls this, and so may a test. A clock that
        stepped backwards past the last attempt counts as due, rather than
        holding fetches off for however far the clock moved.
        """
        # Activity and the due time FIRST, auth.json only after both say go.
        #
        # Found in review, 2026-10-19: this began with fetch_interval(), which
        # opens and parses auth.json before anything else, and the thread
        # calls tick() every TICK_S. So the file that holds the plan key AND
        # every other provider's token OpenCode keeps was read into this
        # process's memory 17,280 times a day -- on a desk idle past
        # GIVE_UP_AFTER_S, where no request would ever be made, and 59 ticks
        # out of 60 on an active one, where none was due. The key is read
        # when it is about to be used and at no other time; that rule is
        # the module docstring's, and this was the one path that broke it.
        interval = self._cadence(now_epoch)
        if interval is None:
            # Not fetching any more -- OpenCode idle past GIVE_UP_AFTER_S, or
            # its data gone -- and so nothing will ever replace or refuse the
            # reading held here. Found in review, 2026-10-19: it was kept,
            # and went on riding the bus as a `stale` frame for as long as
            # the daemon ran; with no fetch to bring a new reset, a weekly
            # window read a week ago was still OpenCode's page. Forgotten
            # instead, as a key that disappears is (below). The first fetch
            # after OpenCode is used again brings a fresh one.
            self._forget()
            return False
        last = self._last_attempt
        if last is not None and 0 <= now_epoch - last < interval:
            return False
        # Stamped before the key is looked for, so a desk with no plan key
        # reads auth.json once per interval, not on every tick until one
        # appears. A key added now waits at most one interval for its first
        # fetch.
        self._last_attempt = now_epoch
        source = quota_source(self._root())
        if source is None:
            # The key is gone -- removed from OpenCode, which is the way the
            # install text names to stop this. The last reading goes with
            # it. Found in review, 2026-10-19: it was kept, and a gauge the
            # person had just switched off went on showing the number it
            # last read, ageing into `stale` but still on the panel, until
            # the reading's own reset times ran out -- up to a week.
            self._forget()
            return False
        quota, err = _fetch(source, now_epoch, self._fetcher, self._root())
        with self._lock:
            if quota is not None:
                self._reading = (quota, now_epoch)
            elif err in (ERR_REJECTED, ERR_NO_KEY):
                # The same review, the other half: the plan REFUSED the key
                # (revoked, rotated, the plan lapsed), or it vanished between
                # the two reads. A failure that says nothing about the key --
                # the network down, a 502 -- keeps the last good reading,
                # because the account behind it still exists and the number
                # is honestly just old. A refused key means the number now
                # describes nothing this desk can see; showing it would be
                # the confident wrong number this project refuses.
                self._reading = None
            self._error = err
        self._report(err)
        return True

    def _forget(self):
        with self._lock:
            self._reading = None
            self._error = None

    def _report(self, err):
        """One stderr line per CHANGE of outcome, in categories only.

        A fetch every minute that fails the same way every minute is one
        line, not 1,440 a day; and it says so again once it works.
        """
        if err == self._reported:
            return
        if err is not None:
            print(f"[opencode] GLM plan quota: {err}", file=sys.stderr)
        elif self._reported is not None:
            print("[opencode] GLM plan quota is being read again",
                  file=sys.stderr)
        self._reported = err

    def _run(self):
        while not self._stop.is_set():
            try:
                self.tick(self._clock())
            except Exception:
                # Nothing is printed: an exception here could only have come
                # from code that touched auth.json, and its text is not
                # something this module vouches for.
                pass
            self._stop.wait(TICK_S)

    def _ensure_thread(self):
        if not self._fetch_enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run,
                                        name="blink-opencode-quota",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        """End the fetcher thread, if one was started. For tests."""
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=FETCH_TIMEOUT_S + TICK_S)

    # --- the frames ---------------------------------------------------------

    def _usage_frame(self, now_epoch):
        reading = self.last_reading()
        if reading is None:
            return None
        quota, fetched_at = reading
        s_pct, s_reset = quota.session_pct, quota.session_resets_at
        w_pct, w_reset = quota.weekly_pct, quota.weekly_resets_at
        # A window whose reset has passed since the fetch is a window that no
        # longer exists. Its percentage describes it and not the one now
        # running, and only a fetch can say what the new one holds -- so it
        # is reported unknown rather than carried across the reset. The
        # normalizer's docstring names the failure: usage that has already
        # been forgiven, shown with confidence.
        #
        # With the clock slack _parse_quota allows (RESET_CLOCK_SLACK_S): a
        # reading it kept because its reset was a second or two behind this
        # desk's fast clock must not be retired by the poll right after it.
        if s_reset is not None and s_reset <= now_epoch - RESET_CLOCK_SLACK_S:
            s_pct, s_reset = base.UNKNOWN, None
        if w_reset is not None and w_reset <= now_epoch - RESET_CLOCK_SLACK_S:
            w_pct, w_reset = base.UNKNOWN, None
        if s_pct < 0 and w_pct < 0:
            return None
        la = last_activity(self._root(), self._state_dir)
        if la is not None:
            # Never later than now. Found in review, 2026-10-19: a file whose
            # mtime is in the future -- a clock that stepped back after the
            # write, a DB copied in from a machine running fast, a `touch`
            # -- made this frame claim to have been used AFTER this poll, and
            # normalizer.select_pair, which hands the second ring to the
            # newest active_at, gave OpenCode the ring over a tool in use
            # this second, for as long as the clock took to catch up. The
            # state frame already clamps the same way
            # (claude_state.activity_at); this one did not.
            la = min(la, now_epoch)
        return base.NormalizedUsageFrame(
            provider=PROVIDER_ID, src=SRC_ID,
            observed_at=fetched_at,
            # See OBSERVED_AT, ACTIVE_AT in the module docstring.
            active_at=la if la is not None else fetched_at,
            session_pct=s_pct, session_resets_at=s_reset,
            weekly_pct=w_pct, weekly_resets_at=w_reset,
            stale=(now_epoch - fetched_at) > STALE_AFTER_S,
        )

    def blank_since(self, now_epoch):
        """OpenCode's last activity when it has run here, else None.

        So that a desk running OpenCode alone, against a model with no plan
        quota, has a page at all: with no session open there is no state
        frame, and without this the bus had nothing to send and the board
        stayed on CONNECTING (ingest.IngestionBus._blank_poll).
        """
        if not present(self._root()):
            return None
        la = last_activity(self._root(), self._state_dir)
        return None if la is None else min(la, now_epoch)

    def poll(self, now_epoch):
        """The usage frame when there is a reading, and the state frame.

        Never blocks on the network and never raises: the fetch happens on
        the thread this starts, and what is read here is its last good
        answer.
        """
        frames = []
        try:
            self._ensure_thread()
        except Exception:
            pass
        try:
            f = self._usage_frame(now_epoch)
            if f is not None:
                frames.append(f)
        except Exception:
            pass
        try:
            counts, names, agents, last = self._state.scan_with_activity(
                now_epoch)
            # The last real event, not `now`: otherwise this frame's
            # active_at tied every other state frame's for an hour and the
            # second ring went by poll order (claude_state.activity_at).
            f = claude_state.frame_from_scan(PROVIDER_ID, STATE_SRC_ID, counts,
                                             names, agents, now_epoch,
                                             last_event_at=last)
            if f is not None:
                frames.append(f)
        except Exception:
            pass
        return frames
