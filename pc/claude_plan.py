"""Is this desk's Claude account on the Ultra plan? From Claude Code's config.

Why it is asked at all. Until 2026-11-01 a desk proved it had no five-hour
window only by Claude Code replying for five and a quarter hours without one
(pc/providers/claude_cli, _reply_without_window), because one weekly-only
reply happens on Pro and Max too: a request still streaming across a reset,
or the last minutes of a window on a clock running fast. The owner's Ultra
desk, which had shown the burn view within minutes the day before, then sat
at `--%` for most of a working day. Knowing the plan rules those Pro and Max
cases out at the source: on a plan that is not Ultra, the fast path is never
taken and the sustained rule is all there is.

WHERE. Claude Code 2.1.316's rule (its y_(), read in the bundle on
2026-11-01): a legacy `.config.json` in the config directory --
$CLAUDE_CONFIG_DIR when that is set, else ~/.claude -- wins when it is a
regular file and not empty; otherwise the file is .claude.json in
$CLAUDE_CONFIG_DIR, or in the home folder when the variable is unset. Not
~/.claude/.claude.json: with the variable unset the config directory and
.claude.json sit side by side. No creator of a non-empty .config.json was
found in 2.1.316, but a desk that kept one from an old release is read
there, as Claude Code reads it. Resolved when called, never at import
(tests/test_sandbox.py fails on the other shape), and like
burn_reader.transcripts_root a login service does not see a variable
exported in a shell profile.

WHAT DECIDES. Two fields of `oauthAccount`: the rate-limit tier,
`organizationRateLimitTier`, which must end in max_20x, and the plan name,
`planDisplayName`, which when present must be "Ultra". Measured on the
owner's Ultra desk on 2026-11-01 (Claude Code 2.1.316, field names and
these two values only): planDisplayName "Ultra", organizationRateLimitTier
"default_claude_max_20x" -- Max 20x being the name the plan had before it
was renamed Ultra. The tier is required, and not the name alone, because
the two are not refreshed alike (the review of PR #6, 2026-11-01): Claude
Code rewrites the tier from its bootstrap call on nearly every launch, but
the plan name only when the stored profile is over a day old, and never
while CLAUDE_CODE_OAUTH_TOKEN supplies the login. So a desk downgraded from
Ultra can name "Ultra" for a day, or indefinitely, beside a current tier
that says otherwise; the tier wins. A name that is not Ultra beside a 20x
tier says the reverse, and is believed too: either one saying "not Ultra"
leaves the desk to the sustained rule.

WHAT IS READ. The whole file, parsed in memory, to reach those two fields.
It holds much more: the account's email address and name, project paths,
and the configuration of every MCP server, whose `env` and `headers` can
carry API tokens; on Linux and Windows it can hold `primaryApiKey` as well.
Of all that, the two fields are looked at and one boolean is kept. Nothing
from the file -- no value, no key, no path, no exception text -- is logged,
printed, raised or saved, at any level (CLAUDE.md), and the parsed document
is dropped before read() returns.

WHAT IT COSTS. A stat once a poll (every two seconds; once a minute
until round 5 of the 2026-11-04 review, which needed a /login seen within
seconds), and a read only when the file's signature -- inode, mtime and
size -- has changed since the last answer, at most once a
FIRST_ANSWER_RETRY_S (ConfigFile). The file was 120 KB on the owner's
desk; a generated 5.6 MB one parses in 11 ms there. One over MAX_BYTES is
not opened at all, and anything but a regular file (a FIFO
would block the open, and the serial thread with it) is not opened either.
Each of those, and JSON that is not an object, reads as "not Ultra", and
that answer is kept against the signature like any other, so it is not
read again until the file changes. Text that does not decode or parse is
not an answer at all (since 9aba323): Claude Code rewrites the file all
day, so it is most often a write caught half-done, and reading it as "not
Ultra, no seat" flipped the page until the file next changed. It is
treated like an OSError or a file that changed between the stat and the
read: the last view is kept and the file is tried again
FIRST_ANSWER_RETRY_S later. Not Ultra is the safe answer: it leaves the
desk to the sustained rule, which is slower and was all there was before.

THE RESIDUAL. One .claude.json vouches for every session in the shared
statusline.json (the review of PR #6, 2026-11-01). The status line is
written by every Claude Code process on the desk, and one run under a
different CLAUDE_CONFIG_DIR -- a second, Pro account used in one terminal
-- renders into the same file while this reader looks at the daemon's
config only. Its weekly-only replies around a reset then take the fast
path on the Ultra account's word, until its next reply renders five_hour
and lifts the mark, after which claude_cli refuses the fast path for a
window. Not redesigned: it needs two accounts on one desk, one of them
Ultra; docs/open-bugs/ultra-burn-followups.md keeps it.
"""
import calendar
import dataclasses
import json
import math
import os
import stat
import time

from pc import claude_account, claude_home

# A lateness no look can help: the plan name in the file can be a day
# stale, or stale for good under CLAUDE_CODE_OAUTH_TOKEN, because Claude
# Code refreshes it only with a profile over a day old (the review of PR
# #6, 2026-11-01). The tier, refreshed on nearly every launch, is what
# keeps a stale name from vouching for a downgraded desk (is_ultra).

# The shortest gap between two parses of the file (ConfigFile.look): a
# change that comes sooner, and a read that failed, wait for it. Not every
# poll: a config Claude Code rewrites every poll, or one that never parses,
# would then be read whole every two seconds on the serial thread. Five
# seconds is how late a /login can be seen for it (round 6 of the
# 2026-11-04 review, items 3 and 4). A source answers from memory and is
# asked at every poll.
FIRST_ANSWER_RETRY_S = 5

# The largest file read. Claude Code's config grows with every project it
# has been run in (a history and settings per folder); 120 KB on the owner's
# desk after months of use. Eight megabytes is sixty times that, and bounds
# what a poll on the serial thread can be made to parse: 11 ms measured for
# 5.6 MB on the owner's Mac (ConfigFile), and only when the file changed,
# at most once a FIRST_ANSWER_RETRY_S. A file over it is not opened: its
# size is the stat's.
MAX_BYTES = 8 * 1024 * 1024

# The plan's name, as planDisplayName gives it, and the tail of the
# rate-limit tier the same plan had before the rename.
ULTRA_NAME = "ultra"
ULTRA_TIER_SUFFIX = "max_20x"

# The legacy file Claude Code still prefers when it is there (WHERE).
LEGACY_NAME = ".config.json"


def config_path(home=None) -> str:
    """Where Claude Code keeps its global config, worked out now, by
    Claude Code 2.1.316's rule (module docstring, WHERE): a non-empty
    regular `.config.json` in $CLAUDE_CONFIG_DIR, or in ~/.claude when that
    is unset; else `.claude.json` in $CLAUDE_CONFIG_DIR, or in the home
    folder. The folder and whether the variable chose it are those of
    `home` (claude_home.current() when None), so the rule is applied to
    the folder `blink install` recorded; which file wins is decided here,
    on every call, and never stored. Costs one stat."""
    home = home or claude_home.current()
    base = (home.config_dir if home.config_dir_set else home.user_dir())
    legacy = home.join(home.config_dir, LEGACY_NAME)
    if claude_home.regular_nonempty(legacy):
        return legacy
    return home.join(base, ".claude.json")


def _tier_is_ultra(tier):
    return (isinstance(tier, str)
            and tier.strip().casefold().endswith(ULTRA_TIER_SUFFIX))


def is_ultra(doc) -> bool:
    """Whether a parsed config names the Ultra plan.

    Both fields have to agree (module docstring, WHAT DECIDES). The tier
    must be present and end in max_20x ("default_claude_max_20x", trimmed,
    any case): max_5x, pro, absent or of another type is not Ultra, whatever
    the name says, because the tier is current and the name may be a day
    old. The name, when it holds one, must be "Ultra" (trimmed, any case):
    "Max", "Pro" or a plan not yet invented beside a 20x tier is not.
    Absent, null or blank, the tier alone decides.
    """
    acct = doc.get("oauthAccount") if isinstance(doc, dict) else None
    if not isinstance(acct, dict):
        return False
    if not _tier_is_ultra(acct.get("organizationRateLimitTier")):
        return False
    name = acct.get("planDisplayName")
    if name is None or (isinstance(name, str) and not name.strip()):
        return True
    return isinstance(name, str) and name.strip().casefold() == ULTRA_NAME


def signature(st):
    """What a stat says about the file, to tell when it has changed: the
    inode, so a file replaced by rename with the same size and mtime is
    still news, the mtime and the size."""
    return (st.st_ino, st.st_mtime_ns, st.st_size)


# What the extra_usage cache is read for, and the seat fields that decide
# the page (docs/plans/enterprise-spend.md, "Recognising a spend seat").
SPEND_SEAT_TIER = "enterprise_usage_based"
SPEND_BILLING_TYPE = "usage_based"


@dataclasses.dataclass(frozen=True)
class SpendAnchor:
    """The spend Claude Code saved at the last /usage, in cents. Raw: not
    adjusted for the month it was fetched in (the caller does that)."""
    used_cents: int
    limit_cents: "int | None"
    enabled: bool
    fetched_at: float


@dataclasses.dataclass(frozen=True)
class ConfigView:
    """Everything this daemon takes from Claude Code's config, from one
    parse. No raw id is held: account_fp is a one-way digest, and org_kind
    is one of claude_account's own words, never the file's string."""
    ultra: bool
    spend_seat: bool
    account_fp: str
    org_kind: str
    mtime: float
    anchor: "SpendAnchor | None"


# No file, or nothing in it that was asked for. account_fp "" is "no
# login to follow" (claude_account.AccountWatch takes it as no news).
EMPTY_VIEW = ConfigView(False, False, "", "consumer", 0.0, None)


def is_spend_seat(doc) -> bool:
    """Whether the login is billed by usage: oauthAccount.seatTier is
    "enterprise_usage_based" or billingType is "usage_based". Only these
    decide the mode; the cache decides the figure, never the mode."""
    acct = doc.get("oauthAccount") if isinstance(doc, dict) else None
    if not isinstance(acct, dict):
        return False
    return (acct.get("seatTier") == SPEND_SEAT_TIER
            or acct.get("billingType") == SPEND_BILLING_TYPE)


# The largest figure an anchor carries, in cents: ten million dollars, the
# bound pc/spend_count and statusline_source put on the same money. A fetch
# time is milliseconds up to 2100-01-01, statusline_source's ceiling on a
# reset stamp. Past either, the cache is not believed: json reads an int of
# any length and a float up to 1e308, and the figure went to the page and
# the wire as it was (the final review of 2026-11-04).
MAX_CENTS = 10 ** 9
MAX_FETCHED_MS = 4_102_444_800 * 1000


def _number(v):
    """A finite int or float that is not a bool, else None.

    The try: math.isfinite converts to float, and an int of 309 digits or
    more raises OverflowError there instead of answering. parse() says it
    never raises, and a used_credits that long made it raise, out of
    PlanReader.look and with it a WSL watch tick and the Ultra check (the
    final review of 2026-11-04, U1)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        return v if math.isfinite(v) else None
    except OverflowError:
        return None


def _anchor(doc):
    """The SpendAnchor in the config's cachedUsageUtilization, or None
    (module docstring of the plan: USD only, finite non-negative amounts,
    a boolean is_enabled, and the cache is this login's when it names one).
    The account comparison happens here and neither id leaves."""
    cache = doc.get("cachedUsageUtilization") if isinstance(doc, dict) \
        else None
    if not isinstance(cache, dict):
        return None
    acct = doc.get("oauthAccount")
    acct = acct if isinstance(acct, dict) else {}
    cached_id = cache.get("accountUuid")
    if cached_id is not None and str(cached_id) != str(
            acct.get("accountUuid")):
        return None
    fetched_ms = _number(cache.get("fetchedAtMs"))
    util = cache.get("utilization")
    extra = util.get("extra_usage") if isinstance(util, dict) else None
    if (fetched_ms is None or not 0 <= fetched_ms <= MAX_FETCHED_MS
            or not isinstance(extra, dict)):
        return None
    currency = extra.get("currency")
    if currency is None:
        currency = "usd"
    if not isinstance(currency, str) or currency.strip().casefold() != "usd":
        return None
    used = _number(extra.get("used_credits"))
    enabled = extra.get("is_enabled")
    if (used is None or not 0 <= used <= MAX_CENTS
            or not isinstance(enabled, bool)):
        return None
    limit = extra.get("monthly_limit")
    if limit is not None:
        limit = _number(limit)
        if limit is None or not 0 <= limit <= MAX_CENTS:
            return None
        limit = int(round(limit))
    return SpendAnchor(int(round(used)), limit, enabled, fetched_ms / 1000.0)


def month_start_utc(now) -> float:
    """Epoch seconds of 00:00 UTC on the 1st of the month `now` is in."""
    t = time.gmtime(now)
    return float(calendar.timegm((t.tm_year, t.tm_mon, 1, 0, 0, 0)))


def next_month_start_utc(now) -> float:
    """Epoch seconds of 00:00 UTC on the 1st of the month after `now`'s."""
    t = time.gmtime(now)
    y, m = (t.tm_year + 1, 1) if t.tm_mon == 12 else (t.tm_year,
                                                      t.tm_mon + 1)
    return float(calendar.timegm((y, m, 1, 0, 0, 0)))


def _view(doc, mtime):
    acct = doc.get("oauthAccount") if isinstance(doc, dict) else None
    acct = acct if isinstance(acct, dict) else {}
    fp = claude_account._fingerprint((acct.get("accountUuid"),
                                      acct.get("organizationUuid")))
    org = acct.get("organizationType")
    kind = (claude_account.ACCOUNT_KINDS.get(org, claude_account.CONSUMER)
            if isinstance(org, str) else claude_account.CONSUMER)
    return ConfigView(ultra=is_ultra(doc), spend_seat=is_spend_seat(doc),
                      account_fp=fp or "", org_kind=kind, mtime=mtime,
                      anchor=_anchor(doc))


def parse(path, st=None):
    """The ConfigView of the file at `path`, or None when it is worth
    trying again: an OSError, or the file changed between the stat and the
    read. A file this reader refuses -- not a regular file, over MAX_BYTES
    -- or one that parses as JSON but is not an object, gives a view with
    nothing in it but the mtime; that answer holds until the file changes.
    Text that will not decode is None, like a half-written read.
    `st` is a stat of `path` already taken (PlanReader's), or None to take
    one. Never raises, and says nothing."""
    try:
        if st is None:
            st = os.stat(path)
        bare = dataclasses.replace(EMPTY_VIEW, mtime=st.st_mtime)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_BYTES:
            return bare                 # not opened: a FIFO would block
        # O_NONBLOCK in case a FIFO took the file's place since the stat:
        # the open then returns at once, and the fstat below refuses it.
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
                     | getattr(os, "O_BINARY", 0))
        with os.fdopen(fd, "rb") as f:
            now = os.fstat(f.fileno())
            # mtime and size, not the inode: Windows reports st_ino by
            # more than one route, and a rename between the stat and the
            # open that kept both is a file no different to read.
            if (not stat.S_ISREG(now.st_mode)
                    or (now.st_mtime_ns, now.st_size)
                    != (st.st_mtime_ns, st.st_size)):
                return None
            raw = f.read(st.st_size + 1)
    except (OSError, ValueError):
        return None
    if len(raw) != st.st_size:
        return None                     # written to while it was read
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        # ValueError covers UnicodeDecodeError and JSONDecodeError;
        # RecursionError is json's answer to an array nested thousands
        # deep, as in session_verdict.load. Not an answer: Claude Code
        # rewrites this file all day, so text that will not decode is most
        # often a write caught half-done, and reading it as "no seat, not
        # Ultra" would flip the page until the file next changed. Retry
        # at the next look and keep the last good view meanwhile.
        return None
    try:
        view = _view(doc, st.st_mtime)
    except (ValueError, TypeError, OverflowError, RecursionError):
        # The backstop for "never raises": every field _view reads is
        # checked for its type, but this is a file anyone can write, and a
        # value no check foresaw must cost the view, not the poll. An
        # answer, like JSON that is not an object: held until the file
        # changes, not read again.
        view = bare
    del doc         # the rest of the file is not kept, not even here
    return view


def read(path, st=None):
    """True or False for Ultra, or None to try again (see parse)."""
    v = parse(path, st)
    return None if v is None else v.ultra


class ConfigFile:
    """One config file's stat, compare, back off and parse, shared by
    PlanReader.look and wsl_watch.WslWatch._look (round 6 of the
    2026-11-04 review, item 7: the watch kept a copy, and the two had
    drifted -- a failed read retried at every tick there, a minute later
    here).

    look() stats the file every time it is asked (under a microsecond on
    the owner's Mac) and parses it when its signature differs from the
    one the last answer came from, at most once a FIRST_ANSWER_RETRY_S: a
    change that comes sooner waits for it, and so does a read that failed
    (a Windows lock, a write caught half done, a file that changed
    between the stat and the read). A parse reads the whole file, and
    Claude Code rewrites it all day; a generated 5.6 MB config parsed in
    11 ms here (median of ten, 2026-11-04), which once a poll on the
    serial thread would be a fifth of a core on a slower machine for
    nothing. A refused file, or JSON that is not an object, is an answer
    (an empty view) and is not read again until it changes."""

    def __init__(self):
        self._sig = None          # the signature the last answer came from
        self._parsed_at = None    # when parse() last ran, for the spacing

    def look(self, path, now_epoch, unreachable=None):
        """A ConfigView when there is a new answer: the file parsed, or
        EMPTY_VIEW when it is not there. None when there is nothing new --
        unchanged, a change or a retry not yet due, a read that failed, a
        stat that failed. `unreachable`, given a FileNotFoundError, says
        whether it is really a share that dropped (wsl_watch), which is
        not knowing, not "no file"."""
        try:
            st = os.stat(path)
        except FileNotFoundError as e:
            if unreachable is not None and unreachable(e):
                return None
            # And the next file to appear is parsed at once: a rewrite by
            # delete and create otherwise waited out the spacing after
            # this "no file", polls of no seat (final review of round 7,
            # F3).
            self._sig = self._parsed_at = None
            return EMPTY_VIEW
        except (OSError, ValueError):
            return None                 # not knowing is not "no seat"
        sig = (path, stat.S_IFMT(st.st_mode)) + signature(st)
        if sig == self._sig:
            return None
        if self._parsed_at is not None:
            since = now_epoch - self._parsed_at
            if since < 0:
                # A time before the last parse: the clock went back. Read
                # as due until the final review of round 7 (F4), so a
                # clock gone back parsed at once. Not due now, and due
                # FIRST_ANSWER_RETRY_S later on the clock as it now reads:
                # waiting for it to catch up with the old reading would
                # hide a /login for as long as the step was.
                self._parsed_at = now_epoch
                return None
            if since < FIRST_ANSWER_RETRY_S:
                return None
        self._parsed_at = now_epoch
        got = parse(path, st)
        if got is not None:
            self._sig = sig
        return got


class PlanReader:
    """The config's view, for one process: the file looked at once a poll
    through a ConfigFile, or `source`, when given, a callable returning a
    ConfigView or None, used instead of the file; None from it keeps the
    last good view."""

    def __init__(self, path=None, source=None, home=None):
        # None: config_path(), asked on every look, so a HOME moved after
        # construction (tests/conftest.py) is followed. `home`, a callable
        # returning a claude_home.ClaudeHome, names whose config that is,
        # asked on every look too; None is claude_home.current().
        self._path = path
        self._home = home
        self._source = source
        self._file = ConfigFile()
        self._view = EMPTY_VIEW
        # The time of the last look: another at the same time is the same
        # poll, and is answered from it (look).
        self._looked_at = None

    def path(self):
        if self._path is not None:
            return self._path
        return config_path(self._home() if self._home is not None else None)

    def look(self, now_epoch) -> ConfigView:
        """The view now.

        Looked at once a poll, and not once a minute as until round 5
        of the 2026-11-04 review (ruling A): the status line is read every
        two seconds, and a /login seen up to a minute late left the new
        login's renders counted on the old one and its first render taken
        for the old login's (claude_cli._note_login). Once a poll: the
        provider, the login watch, the bus's spend check and ultra() each
        ask, and looks at one poll's time share the first (round 6, item
        4). The file's spacing and retries are ConfigFile's. A source
        answers from memory and is asked at every poll.

        Before a first answer the view held is EMPTY_VIEW, "no seat, not
        Ultra"; a read that fails then is tried again FIRST_ANSWER_RETRY_S
        later like any other (/code-review max of 2026-11-04, U18: a
        minute of Claude Desktop's windows on a spend desk after every
        daemon start)."""
        if self._looked_at is not None and now_epoch == self._looked_at:
            return self._view
        self._looked_at = now_epoch
        if self._source is not None:
            got = self._source()
        else:
            try:
                p = self.path()
            except (OSError, ValueError):
                return self._view       # not knowing is not "no seat"
            got = self._file.look(p, now_epoch)
        if got is not None:
            self._view = got
        return self._view

    def ultra(self, now_epoch) -> bool:
        return self.look(now_epoch).ultra
