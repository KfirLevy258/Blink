"""Keeping one Claude login's figures off another login's page.

Claude meters people two ways. Pro and Max get a five-hour window and a
seven-day one. An Enterprise seat behind a Claude apps gateway gets a dollar
cap instead (pc/statusline_source._spend_frame), and an Enterprise seat that
signs in to claude.ai directly reports nothing this daemon can read.

gapietro's fork was written for a person who holds both kinds and moves
between them with /login several times a day (2026-10-14). That move is the
hazard. Nothing this daemon reads is cleared by a login: the status line,
Claude Desktop's cache and the weekly anchor all go on holding the previous
account's figures until something writes over them, and the normalizer would
happily merge a Max session window into the page of a login that has none.

What is read, and from where: the `oauthAccount` object Claude Code keeps in
~/.claude.json (or under CLAUDE_CONFIG_DIR), and from it only the
organisation type and the two account ids. That object describes the login;
it is not a credential, which Claude Code keeps in the Keychain or in
~/.claude/.credentials.json and which nothing here opens. The ids are hashed
on the way in and only the hash is kept, so this process never holds them.

Nothing taken from the file is printed or logged, at any level: not the
organisation type, not the hash, not a fragment of a file that would not
parse. The rest of the file -- Claude Code's per-project history -- is parsed
because JSON must be, and dropped with the document.

HELD FROM RELEASE. pc/ingest consults this only after a spend reading has
appeared on the bus, so on a desk whose status line has never carried a
dollar cap the file is never opened. The organisation-type strings below are
Claude Code's as documented; nobody has read them off an Enterprise profile.
"""
import collections
import hashlib
import json
import os

from pc.providers import base

PROVIDER_ID = "claude"

# Organisation type -> the word the board shows. Every other value, including
# the "claude_max" seen on a Max desk and any type Claude Code adds later, is
# CONSUMER, which leaves the page exactly as it was before plans existed. An
# unknown type taken for Enterprise would paint "Not reported" over somebody's
# working Max figures; taken for a consumer, the worst it does is nothing.
ACCOUNT_KINDS = {
    "claude_enterprise": "enterprise",
    "claude_team": "team",
}
CONSUMER = "consumer"

# The most of the file that is ever read. Measured on the development Mac on
# 2026-10-27 the file was 114 KB, after months of use; five megabytes is far
# past any honest profile and still a parse nobody would notice on the 2 s
# tick. A larger file is not read at all.
PROFILE_MAX_BYTES = 5 * 1024 * 1024

# What a readable profile yields. `fingerprint` changes when the account does
# and says nothing about which account it is.
Profile = collections.namedtuple("Profile", "kind fingerprint")


def profile_path() -> str:
    """The profile's location, worked out at the call.

    At the call because tests/conftest.py moves HOME after import, and a path
    computed at import would point every test at the real file -- the one gap
    in the sandbox that CLAUDE.md warns about. CLAUDE_CONFIG_DIR is honoured
    because Claude Code honours it, and a desk that uses it would otherwise
    never be seen to log in at all.
    """
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    folder = os.path.expanduser(config_dir) if config_dir else \
        os.path.expanduser("~")
    return os.path.join(folder, ".claude.json")


def _fingerprint(ids):
    """A stable digest of the account ids, or None when there are none.

    Only strings count. A profile with neither id is one this cannot follow
    across a switch, and is treated as no profile rather than as an account
    whose identity is "nothing".
    """
    present = [i for i in ids if isinstance(i, str) and i]
    if not present:
        return None
    joined = "\x1f".join(i if isinstance(i, str) else "" for i in ids)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def read_profile(path=None):
    """The signed-in account as a Profile, or None if it cannot be told.

    None covers a missing file, one over PROFILE_MAX_BYTES, one half-written,
    one that is not UTF-8 or not JSON, and an API-key login with no
    oauthAccount. Callers take None to mean "no news", which is the right
    reading of a file that Claude Code is in the middle of rewriting.

    The bound is enforced on what is actually read -- one byte past the
    limit is requested, and getting it means the file is too big -- rather
    than on a stat taken a moment earlier of a file that may since have
    grown.
    """
    try:
        with open(path or profile_path(), "rb") as f:
            raw = f.read(PROFILE_MAX_BYTES + 1)
    except OSError:
        return None
    if len(raw) > PROFILE_MAX_BYTES:
        return None
    try:
        doc = json.loads(raw.decode("utf-8"))
    except ValueError:      # UnicodeDecodeError is a ValueError too
        return None
    oauth = doc.get("oauthAccount") if isinstance(doc, dict) else None
    if not isinstance(oauth, dict):
        return None
    fp = _fingerprint((oauth.get("accountUuid"),
                       oauth.get("organizationUuid")))
    if fp is None:
        return None
    org = oauth.get("organizationType")
    return Profile(ACCOUNT_KINDS.get(org, CONSUMER) if isinstance(org, str)
                   else CONSUMER, fp)


def profile_kind(path=None):
    """"enterprise", "team", "consumer", or None when unreadable."""
    prof = read_profile(path)
    return None if prof is None else prof.kind


class AccountWatch:
    """Follows the profile, and remembers when the account in it changed.

    The file's mtime is no clue to a login: Claude Code rewrites it for its
    own bookkeeping all day. So a switch is dated by the poll on which this
    watch first sees a different fingerprint, which on the daemon's two-second
    tick is at most a tick after /login.

    The fingerprint found on the first readable poll is a starting point, not
    a switch. A watch that has only just started has no idea when that login
    began, and dating it "now" would discard every reading on the desk at
    each daemon restart, the current account's included. What that costs: a
    switch made while the daemon was down is never dated. select() still
    lets the freshest reading choose the plan, so the page comes right as
    soon as the new account is used.

    Parsing happens only when the file's (size, mtime) has moved since the
    last look. At 1800 polls an hour against a file of a hundred kilobytes
    and more, re-reading every time would be almost all waste.
    """

    def __init__(self, path=None):
        self._path = path
        self._seen_stat = None
        self._fingerprint = None
        self.kind = ""
        self.since = None
        # When Claude Code last touched the file, which is evidence that it
        # was running then. Used as the active_at of an unreported frame.
        self.touched_at = None

    def _fresh_stat(self, path):
        """The file's stat when it differs from the last one looked at."""
        try:
            st = os.stat(path)
        except OSError:
            return None
        key = (st.st_size, st.st_mtime_ns)
        if key == self._seen_stat:
            return None
        self._seen_stat = key
        return st

    def _adopt(self, prof, now_epoch):
        switched = (self._fingerprint is not None
                    and prof.fingerprint != self._fingerprint)
        if switched:
            self.since = now_epoch
        self._fingerprint = prof.fingerprint
        self.kind = prof.kind

    def poll(self, now_epoch):
        path = self._path or profile_path()
        st = self._fresh_stat(path)
        if st is None:
            return
        self.touched_at = st.st_mtime
        prof = read_profile(path)
        if prof is not None:
            self._adopt(prof, now_epoch)


def _speaks_for_account(f):
    """Whether a claude frame makes a claim about the account's allowance:
    a percentage, a plan, or a window seen to end. A frame carrying only the
    hook's state and counts is about a terminal, and no login changes it."""
    return (f.has_usage() or f.plan != base.PLAN_WINDOWS
            or f.session_rolled_at is not None
            or f.weekly_rolled_at is not None)


def _same_plan_as_newest(readings):
    """Drop every reading whose plan differs from the newest reading's.

    Two plans on one page would be two accounts on one page: a Max weekly
    window beside an Enterprise spend share, each true of a different login.
    Spend and not-spend is the split that matters; an unreported frame sides
    with the windows, since it carries no figure to mix.
    """
    if not readings:
        return readings
    want_spend = max(readings, key=lambda f: f.observed_at).plan \
        == base.PLAN_SPEND
    return [f for f in readings if (f.plan == base.PLAN_SPEND) == want_spend]


def _unreported(kind, now, touched_at):
    """The frame that says an Enterprise or Team login reports nothing.

    Observed now: the statement is true of this poll, and dating it earlier
    would caption it as an old reading. Active at the profile's mtime, never
    later than now, because the board dozes on active_at and a sentence this
    daemon repeats to itself is not a sign of anybody at the desk.
    """
    active = now if touched_at is None else min(now, touched_at)
    return base.NormalizedUsageFrame(
        provider=PROVIDER_ID, src="account", observed_at=now,
        active_at=active, plan=base.PLAN_UNREPORTED, account=kind)


def select(frames, now, kind="", since=None, touched_at=None):
    """This poll's frames, less whatever belongs to another Claude login.

    Other providers, and claude frames that speak only for a terminal, are
    returned as they came. Of the rest:

      - a reading observed before `since` is from the previous login, and
        goes;
      - of what is left, only readings on the newest one's plan stay;
      - and if that leaves nothing with a figure on an Enterprise or Team
        login, one unreported frame is added so the page can say so. A Team
        login that does report windows keeps them: a frame with figures is
        never swapped for one that says there are none.
    """
    passing = [f for f in frames
               if f is not None and (f.provider != PROVIDER_ID
                                     or not _speaks_for_account(f))]
    readings = [f for f in frames
                if f is not None and f.provider == PROVIDER_ID
                and _speaks_for_account(f)
                and (since is None or f.observed_at >= since)]
    readings = _same_plan_as_newest(readings)
    has_figure = any(f.has_usage() for f in readings)
    if not has_figure and kind in ("enterprise", "team"):
        readings = [_unreported(kind, now, touched_at)]
    return passing + readings
