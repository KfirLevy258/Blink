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
its config, parsed once by pc/claude_plan (which knows where the file is),
and from it only the organisation type and the two account ids. That object describes the login;
it is not a credential, which Claude Code keeps in the Keychain or in
~/.claude/.credentials.json and which nothing here opens. The ids are hashed
on the way in and only the hash is kept, so this process never holds them.

Nothing taken from the file is printed or logged, at any level: not the
organisation type, not the hash, not a fragment of a file that would not
parse. The rest of the file -- Claude Code's per-project history -- is parsed
because JSON must be, and dropped with the document.

HELD FROM RELEASE. pc/ingest consults this only after a spend reading has
appeared on the bus -- a gateway's dollar cap on the status line, or the
spend page claude_cli builds on a seat billed by usage. The config itself
is looked at on every desk now, at every poll, for the plan
and the seat (docs/plans/enterprise-spend.md, 1); until a spend reading
appears no frame is filtered here. The organisation-type strings below are
Claude Code's as documented; nobody has read them off an Enterprise profile.
"""
import hashlib

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
    # surrogatepass: json reads a lone surrogate ("\ud800") into a str
    # that plain UTF-8 refuses with UnicodeEncodeError, and that made
    # claude_plan.parse, which never raises, raise (the final review of
    # 2026-11-04). Real ids are ASCII and digest exactly as before; a
    # surrogate digests to its own stable value.
    raw = joined.encode("utf-8", "surrogatepass")
    return hashlib.sha256(raw).hexdigest()


class AccountWatch:
    """Follows the profile, and remembers when the account in it changed.

    The file's mtime alone is no clue to a login: Claude Code rewrites it
    for its own bookkeeping all day. So a switch is noticed by the poll on
    which this watch first sees a different fingerprint. The view comes from
    pc/claude_plan's PlanReader, which stats the file at every look (round
    5 of the 2026-11-04 review; once a RECHECK_S before), so a switch is
    seen on the poll after /login writes it.

    But it is DATED by the file. /login writes the file, so the switch
    happened no later than the mtime of the view that first carries the new
    fingerprint, and after the mtime of the last view that carried the old
    one. That mtime is `since` when it falls between the two and is not in
    the future; otherwise the poll's own time, as before. Dated by the poll
    alone, `since` was up to a minute late, and select() threw out the new
    login's first readings as the old login's: "Not reported" on an
    Enterprise page until its next render (/code-review max of 2026-11-04,
    finding 9). A later rewrite between /login and the look can still put
    the mtime after the switch; that is the old lateness, smaller.

    The fingerprint found on the first readable poll is a starting point, not
    a switch. A watch that has only just started has no idea when that login
    began, and dating it "now" would discard every reading on the desk at
    each daemon restart, the current account's included. What that costs: a
    switch made while the daemon was down is never dated. select() still
    lets the freshest reading choose the plan, so the page comes right as
    soon as the new account is used.

    The file is not read here. pc/claude_plan parses Claude Code's config
    once, paced and cached against its signature, and `view_source(now)`
    returns that parse's ConfigView; the fingerprint, the organisation kind
    and the file's mtime come from it. A view with no fingerprint (no file,
    no login, a file that would not parse) is no news.
    """

    def __init__(self, view_source):
        self._view_source = view_source
        self._fingerprint = None
        # The mtime of the last view that carried self._fingerprint: the
        # earliest a switch away from it can be dated (see above).
        self._fp_mtime = None
        self._seen_mtime = None
        self.kind = ""
        self.since = None
        # When Claude Code last touched the file, which is evidence that it
        # was running then. Used as the active_at of an unreported frame.
        self.touched_at = None

    def poll(self, now_epoch):
        view = self._view_source(now_epoch)
        if view.mtime and view.mtime != self._seen_mtime:
            self._seen_mtime = view.mtime
            self.touched_at = view.mtime
        fp = view.account_fp
        if not fp:
            return
        at = view.mtime or None
        if self._fingerprint is not None and fp != self._fingerprint:
            self.since = now_epoch
            if (at is not None and at <= now_epoch
                    and (self._fp_mtime is None or at > self._fp_mtime)):
                self.since = at
        self._fingerprint = fp
        self._fp_mtime = at
        self.kind = view.org_kind


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
        goes -- except the spend page claude_cli builds from the config
        (base.SPEND_SRC_ID), which is computed for the login the config
        names now, at every poll, and is dated by the last render, which
        can well be older than the switch;
      - of what is left, only readings on the newest one's plan stay;
      - and if that leaves nothing with a figure on an Enterprise or Team
        login, one unreported frame is added so the page can say so. A Team
        login that does report windows keeps them: a frame with figures is
        never swapped for one that says there are none, and a spend page
        with dollars is such a frame even with no share (base.has_usage).
    """
    passing = [f for f in frames
               if f is not None and (f.provider != PROVIDER_ID
                                     or not _speaks_for_account(f))]
    readings = [f for f in frames
                if f is not None and f.provider == PROVIDER_ID
                and _speaks_for_account(f)
                and (since is None or f.observed_at >= since
                     or (f.plan == base.PLAN_SPEND
                         and f.src == base.SPEND_SRC_ID))]
    readings = _same_plan_as_newest(readings)
    has_figure = any(f.has_usage() for f in readings)
    if not has_figure and kind in ("enterprise", "team"):
        readings = [_unreported(kind, now, touched_at)]
    return passing + readings
