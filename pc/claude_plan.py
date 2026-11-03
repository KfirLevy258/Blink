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

WHAT IT COSTS. A stat at most once every RECHECK_S, and a read only when
the file's signature -- inode, mtime and size -- has changed since the last
look that got an answer. The file was 120 KB on the owner's desk. One over
MAX_BYTES is not opened at all, and anything but a regular file (a FIFO
would block the open, and the serial thread with it) is not opened either.
Each of those, and a file corrupt or of any other shape, reads as "not
Ultra", and that answer is kept against the signature like any other, so
it is not parsed again until the file changes. Only an OSError, or a file
that changed between the stat and the read, is tried again at the next
look. Not Ultra is the safe answer: it leaves the desk to the sustained
rule, which is slower and was all there was before.

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

from pc import claude_account

# How often the file is looked at, at most. The plan changes only on an
# upgrade or a downgrade, and a minute late on either is nothing against
# the five and a quarter hours this path saves. A minute late is not the
# only lateness, though: the plan name in the file can be a day stale, or
# stale for good under CLAUDE_CODE_OAUTH_TOKEN, because Claude Code
# refreshes it only with a profile over a day old (the review of PR #6,
# 2026-11-01). The tier, refreshed on nearly every launch, is what keeps a
# stale name from vouching for a downgraded desk (is_ultra).
RECHECK_S = 60

# The largest file read. Claude Code's config grows with every project it
# has been run in (a history and settings per folder); 120 KB on the owner's
# desk after months of use. Eight megabytes is sixty times that, and bounds
# what a poll on the serial thread can be made to parse: tens of
# milliseconds, at most once a RECHECK_S and only when the file changed. A
# file over it is not opened: its size is the stat's.
MAX_BYTES = 8 * 1024 * 1024

# The plan's name, as planDisplayName gives it, and the tail of the
# rate-limit tier the same plan had before the rename.
ULTRA_NAME = "ultra"
ULTRA_TIER_SUFFIX = "max_20x"

# The legacy file Claude Code still prefers when it is there (WHERE).
LEGACY_NAME = ".config.json"


def config_path(environ=None) -> str:
    """Where Claude Code keeps its global config, worked out now, by
    Claude Code 2.1.316's rule (module docstring, WHERE): a non-empty
    regular `.config.json` in $CLAUDE_CONFIG_DIR, or in ~/.claude when that
    is unset; else `.claude.json` in $CLAUDE_CONFIG_DIR, or in the home
    folder. `environ` is os.environ when None, as in
    burn_reader.transcripts_root. Costs one stat."""
    base = (os.environ if environ is None else environ).get(
        "CLAUDE_CONFIG_DIR")
    if base:
        base = os.path.expanduser(base)
        legacy_dir = base
    else:
        base = os.path.expanduser("~")
        legacy_dir = os.path.join(base, ".claude")
    legacy = os.path.join(legacy_dir, LEGACY_NAME)
    try:
        st = os.stat(legacy)
        if stat.S_ISREG(st.st_mode) and st.st_size > 0:
            return legacy
    except (OSError, ValueError):
        pass
    return os.path.join(base, ".claude.json")


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


def _number(v):
    """A finite int or float that is not a bool, else None."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return v if math.isfinite(v) else None


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
    if fetched_ms is None or fetched_ms < 0 or not isinstance(extra, dict):
        return None
    currency = extra.get("currency")
    if currency is None:
        currency = "usd"
    if not isinstance(currency, str) or currency.strip().casefold() != "usd":
        return None
    used = _number(extra.get("used_credits"))
    enabled = extra.get("is_enabled")
    if used is None or used < 0 or not isinstance(enabled, bool):
        return None
    limit = extra.get("monthly_limit")
    if limit is not None:
        limit = _number(limit)
        if limit is None or limit < 0:
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
    -- or one that does not parse or has the wrong shape gives a view with
    nothing in it but the mtime; that answer holds until the file changes.
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
        # deep, as in session_verdict.load.
        return bare
    view = _view(doc, st.st_mtime)
    del doc         # the rest of the file is not kept, not even here
    return view


def read(path, st=None):
    """True or False for Ultra, or None to try again (see parse)."""
    v = parse(path, st)
    return None if v is None else v.ultra


class PlanReader:
    """The config's view, for one process: stat-throttled, cached against
    the file's signature. `source`, when given, is a callable returning a
    ConfigView or None, used instead of the file; None from it keeps the
    last good view."""

    def __init__(self, path=None, source=None):
        # None: config_path(), asked on every look, so a HOME moved after
        # construction (tests/conftest.py) is followed.
        self._path = path
        self._source = source
        self._looked_at = None
        self._sig = None
        self._view = EMPTY_VIEW

    def path(self):
        return self._path if self._path is not None else config_path()

    def look(self, now_epoch) -> ConfigView:
        """The view as of the last look. A look is due RECHECK_S after the
        last one, or at once if the clock has gone back past it."""
        if (self._looked_at is not None
                and 0 <= now_epoch - self._looked_at < RECHECK_S):
            return self._view
        self._looked_at = now_epoch
        if self._source is not None:
            got = self._source()
            if got is not None:
                self._view = got
            return self._view
        try:
            p = self.path()
            st = os.stat(p)
        except FileNotFoundError:
            self._sig, self._view = None, EMPTY_VIEW
            return self._view
        except (OSError, ValueError):
            return self._view           # not knowing is not "no seat"
        sig = (p, stat.S_IFMT(st.st_mode)) + signature(st)
        if sig == self._sig:
            return self._view
        got = parse(p, st)
        # Only a read worth trying again is not cached against the stamp:
        # a Windows lock, or a write caught half-done. A refused, corrupt
        # or misshapen file is kept as an empty view until it changes, so
        # it is not parsed again once a minute.
        if got is None:
            self._sig = None
            return self._view
        self._sig, self._view = sig, got
        return self._view

    def ultra(self, now_epoch) -> bool:
        return self.look(now_epoch).ultra
