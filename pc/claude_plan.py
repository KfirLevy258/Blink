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

WHERE. Claude Code keeps its global config in $CLAUDE_CONFIG_DIR/.claude.json
when that is set, else ~/.claude.json: `join(CLAUDE_CONFIG_DIR || homedir(),
".claude.json")`, read in Claude Code 2.1.316's bundle on 2026-11-01. Not
~/.claude/.claude.json: the config directory and the file sit side by side
when the variable is unset. Resolved when called, never at import
(tests/test_sandbox.py fails on the other shape), and like
burn_reader.transcripts_root a login service does not see a variable
exported in a shell profile.

WHAT IS TAKEN. Two fields of `oauthAccount` and nothing else:
`planDisplayName`, and when that is absent `organizationRateLimitTier`.
Measured on the owner's Ultra desk on 2026-11-01 (Claude Code 2.1.316,
field names and these two values only): planDisplayName "Ultra",
organizationRateLimitTier "default_claude_max_20x" -- Max 20x being the name
the plan had before it was renamed Ultra, which is what an older Claude Code
that never wrote planDisplayName leaves behind. The file also holds the
account's email address and name, project paths and more. The whole file has
to be parsed to reach two fields, but what is kept is one boolean; nothing
from the file -- no value, no key, no path, no exception text -- is logged,
printed, raised or saved, at any level (CLAUDE.md).

WHAT IT COSTS. A stat at most once every RECHECK_S, and a read only when the
file's mtime or size has changed since the last read that parsed. The file
was 120 KB on the owner's desk; one over MAX_BYTES is not read at all, and
reads as "not Ultra", as does one missing, unreadable, corrupt or of any
other shape. Not Ultra is the safe answer: it leaves the desk to the
sustained rule, which is slower and was all there was before.
"""
import json
import os

# How often the file is looked at, at most. Claude Code rewrites it on most
# runs, but the plan in it changes only on an upgrade or a downgrade, and
# a minute late on either is nothing against the five and a quarter hours
# this path saves.
RECHECK_S = 60

# The largest file read. Claude Code's config grows with every project it
# has been run in (a history and settings per folder); 120 KB on the owner's
# desk after months of use. Eight megabytes is sixty times that, and bounds
# what a poll on the serial thread can be made to parse: tens of
# milliseconds, at most once a RECHECK_S and only when the file changed.
MAX_BYTES = 8 * 1024 * 1024

# The plan's name, as planDisplayName gives it, and the tail of the
# rate-limit tier the same plan had before the rename.
ULTRA_NAME = "ultra"
ULTRA_TIER_SUFFIX = "max_20x"


def config_path(environ=None) -> str:
    """Where Claude Code keeps .claude.json, worked out now. `environ` is
    os.environ when None, as in burn_reader.transcripts_root."""
    base = (os.environ if environ is None else environ).get(
        "CLAUDE_CONFIG_DIR")
    base = os.path.expanduser(base) if base else os.path.expanduser("~")
    return os.path.join(base, ".claude.json")


def is_ultra(doc) -> bool:
    """Whether a parsed .claude.json names the Ultra plan.

    planDisplayName decides when it holds a name: "Ultra", trimmed and in
    any case, and anything else ("Max", "Pro", a plan not yet invented) is
    not. Absent, null or blank, the rate-limit tier decides, and only a
    tier ending in max_20x is Ultra: max_5x, pro and the rest are not.
    """
    acct = doc.get("oauthAccount") if isinstance(doc, dict) else None
    if not isinstance(acct, dict):
        return False
    name = acct.get("planDisplayName")
    if name is not None and not (isinstance(name, str) and not name.strip()):
        return isinstance(name, str) and name.strip().casefold() == ULTRA_NAME
    tier = acct.get("organizationRateLimitTier")
    return (isinstance(tier, str)
            and tier.strip().casefold().endswith(ULTRA_TIER_SUFFIX))


def read(path):
    """True or False for the file at `path`, or None when it could not be
    read or parsed. Never raises, and says nothing."""
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_BYTES + 1)
    except (OSError, ValueError):
        return None
    if len(raw) > MAX_BYTES:
        return None
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        # ValueError covers UnicodeDecodeError and JSONDecodeError;
        # RecursionError is json's answer to an array nested thousands
        # deep, as in session_verdict.load.
        return None
    ultra = is_ultra(doc)
    del doc         # the rest of the file is not kept, not even here
    return ultra


class PlanReader:
    """The plan, for one process: stat-throttled, mtime-cached."""

    def __init__(self, path=None):
        # None: config_path(), asked on every look, so a HOME moved after
        # construction (tests/conftest.py) is followed.
        self._path = path
        self._looked_at = None
        self._sig = None
        self._ultra = False

    def path(self):
        return self._path if self._path is not None else config_path()

    def ultra(self, now_epoch) -> bool:
        """Whether the account is on Ultra, as of the last look. A look is
        due RECHECK_S after the last one, or at once if the clock has gone
        back past it."""
        if (self._looked_at is not None
                and 0 <= now_epoch - self._looked_at < RECHECK_S):
            return self._ultra
        self._looked_at = now_epoch
        try:
            p = self.path()
            st = os.stat(p)
        except (OSError, ValueError):
            self._sig, self._ultra = None, False
            return False
        sig = (p, st.st_mtime_ns, st.st_size)
        if sig == self._sig:
            return self._ultra
        got = read(p)
        # A failed read is not cached against the file's stamp: a Windows
        # lock or a write caught half-done is tried again at the next look.
        self._sig = sig if got is not None else None
        self._ultra = bool(got)
        return self._ultra
