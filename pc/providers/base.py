"""The contract every provider implements, and the frame they all produce.

A provider is one AI tool we can learn usage from -- Claude today, Codex or
anything else later. A source is one *way* of learning it from that tool: the
CLI hook, an app's own cache file, a rollout log. One provider owns several
sources, and the sources disagree, so everything below is shaped by two
facts:

  - Every source is optional and every source can be absent, old, or wrong in
    a way it cannot detect. Nothing here may assume a reading exists.
  - The board must never be handed a confident number that came from a guess.
    That is why -1.0 is the unknown, not 0.0 -- 0.0 renders as "0% used",
    which is a stronger claim than "we don't have this". pc/statusline_source
    established that convention and the firmware already defaults to -1 on an
    absent key (proto.c, msg_get_double); this keeps one rule across all of it.
"""
import dataclasses
import typing

# Percentages and context fullness use this when we simply do not know.
# Deliberately the same sentinel the wire protocol and the firmware already
# use, so a value can travel from a parser to a pixel without being
# re-encoded on the way.
UNKNOWN = -1.0

# The execution states in the doc's live state machine. "" means this source
# has nothing to say about execution state, which is different from "idle" --
# idle is a claim, "" is a shrug, and only one of them should ever turn a
# light on.
STATE_UNKNOWN = ""
STATE_IDLE = "idle"
STATE_RUNNING = "running"
STATE_WAITING = "waiting"
STATE_STUCK = "stuck"
# A turn that ended on an API error rather than on an answer. Its own state
# because StopFailure carries `error: "rate_limit"` among its causes, and on a
# usage gauge being rate limited is the headline rather than a detail.
STATE_FAILED = "failed"

VALID_STATES = (STATE_UNKNOWN, STATE_IDLE, STATE_RUNNING, STATE_WAITING,
                STATE_STUCK, STATE_FAILED)

# Worst-first. This is the order the single scalar `state` collapses to when
# several sessions -- of one provider or of both -- disagree, and the order is
# a product decision rather than an implementation detail.
#
# `failed` outranks `stuck` because a rate limit is the one condition this
# gauge exists to surface, and a wedged tool is a distant second.
#
# `idle` outranks `running`, which reads backwards until you ask what the
# light is FOR. A session that finished its turn is a session waiting on the
# person; a session still working needs nothing from them. So "one finished,
# two still working" is a light that says "your turn", not one that says
# "busy" -- the busy ones will still be busy when the person gets there. It
# used to be the other way round, and a finished answer sat unnoticed behind
# a green pulse for as long as anything else was running (user decision
# 2026-08-29).
SEVERITY = (STATE_FAILED, STATE_STUCK, STATE_WAITING, STATE_IDLE,
            STATE_RUNNING)


def worst_of(states):
    """The state a single indicator should show for several sessions at once."""
    for s in SEVERITY:
        if s in states:
            return s
    return STATE_UNKNOWN


@dataclasses.dataclass
class NormalizedUsageFrame:
    """One reading, from one source, about one provider.

    This is the only type that crosses from provider code into the normalizer,
    which is the point: adding Codex must not require the normalizer, the
    protocol layer or the firmware to learn anything new. If a future provider
    cannot express itself in these fields, that is a signal to extend this
    class once rather than to special-case it downstream.

    `observed_at` is when the UNDERLYING DATA was written, not when we read it.
    The difference matters more than it looks: reading a two-day-old file at
    12:00 must not make it look like a 12:00 reading, and every freshness and
    conflict decision downstream is built on this field being honest about it.
    """
    provider: str
    src: str
    observed_at: float
    # The freshest thing this frame stands on, which is a different question
    # from `observed_at` above and only looks like the same one.
    #
    # `observed_at` is the age of the NUMBER: which reading the dial is
    # showing. This is the age of the CONTACT: when the tool behind it last
    # said anything at all, whether or not what it said carried a percentage.
    # For a single reading the two are one value. For a merged frame this is
    # the newest observed_at of everything the merge saw, INCLUDING the
    # sources that lost every field -- they lost the contest for the dial,
    # not the argument that somebody is at this machine.
    #
    # They came apart when ClaudeCliProvider learned to re-offer the last
    # payload that had a five-hour window. Claude Code rewrites its status
    # line every minute, the expired five-hour figure is simply absent from
    # the rewrite, and the remembered payload -- carrying its own original
    # mtime -- wins the session dial and hands the merged frame its age. The
    # dial is honestly six hours old and the desk is honestly occupied. Both
    # facts are true and only one of them is about the person, so the board
    # needs to be told both (see protocol.usage's `active_age_s`).
    #
    # None means "the same as observed_at", filled in below, so no provider
    # has to know this field exists and any frame is a valid input to merge().
    active_at: object = None
    session_pct: float = UNKNOWN
    session_resets_at: object = None
    weekly_pct: float = UNKNOWN
    weekly_resets_at: object = None
    state: str = STATE_UNKNOWN
    stale: bool = False
    # How fast the session window is filling, in percent per hour, or None.
    #
    # Only a source with HISTORY can answer this, which today is the desktop
    # cache alone. It exists for the one configuration that has percentages
    # and no reset time -- Claude Desktop without Claude Code -- where the
    # countdown has nothing to show. It is a measurement of the user's own
    # consumption over samples we actually observed, not an inference about
    # when a window rolls: this project already refused that (see
    # pc/providers/claude_desktop.session_burn_pph for the numbers).
    #
    # None, not 0.0, for absent. A zero rate is a real answer ("you have
    # stopped") and must not be spelled the same way as "we cannot tell".
    session_burn_pph: object = None
    # WHEN a window was observed to reset, if this source saw it happen.
    #
    # Not a duplicate of `*_resets_at`, which is when the window will roll
    # NEXT. This is evidence about the past: at this epoch the window emptied,
    # so any reading of it taken earlier describes a window that no longer
    # exists. Only a source that can see reset timestamps can ever set it:
    # the Claude status line and the Codex rollout through settle_window()
    # below, and Claude Desktop's Local Storage through window_ended()
    # with its own answer for the percentage. pc/normalizer uses it to stop
    # an older reading from another source outliving the reset it never knew
    # about.
    session_rolled_at: object = None
    weekly_rolled_at: object = None
    # How many live sessions are in each state, and how many subagents are
    # running across all of them. `state` above is the worst of these, for a
    # single indicator; these are what a list would be built from.
    #
    # Counts rather than a list of sessions, and that is a wire decision
    # reaching back into the frame: the board drops an over-long line whole
    # (proto.c) and a per-session array blows the 512-byte budget at around
    # four sessions -- taking the panel dark with no error, on exactly the
    # busy machine most likely to have four.
    n_run: int = 0
    n_wait: int = 0
    n_stuck: int = 0
    n_idle: int = 0
    n_agents: int = 0
    # Which project the ONE session in `state` belongs to, when there is
    # exactly one. Empty when several share the state -- see
    # claude_state.poll for why naming one of several is refused rather than
    # guessed.
    #
    # TWO providers set this now, by different routes, and the difference
    # matters to anyone debugging a missing name. claude_state gets it from
    # the hook, which is told the project directly. codex_cli derives it from
    # the `cwd` on the first line of a rollout file, so a Codex name can be
    # absent for reasons the Claude path never has -- an unreadable head, a
    # cwd that sanitises to nothing. Every other source leaves it empty and
    # the board falls back to the count.
    label: str = ""
    # The age of the FIGURES, for a source whose file can be younger than
    # what it says. Ranking percentages by observed_at assumes that writing
    # the file and fetching the numbers in it happen together, and for the
    # status line they do not: a repaint stamps a new mtime on an old
    # rate_limits block. A repainted 39 then outranked a Claude Desktop 40
    # sampled after the 39 was fetched, and the panel went 39, 40, 39, 41
    # (docs/open-bugs/usage-merge-jitter.md). pc/normalizer ranks the two
    # percentages by this field instead.
    #
    # ClaudeCliProvider sets it to the oldest mtime its block has appeared
    # under. No other source sets it, and None is filled in below as
    # observed_at, so for them nothing changes. It is clamped so it is never
    # after observed_at. observed_at itself, and every age derived from it
    # (the wire's age_s, staleness), mean what they always meant.
    first_seen_at: object = None
    # The claude page has NO session window: this account, as of this
    # frame, has no five-hour limit to show. Verified Ultra accounts
    # (2026-10-30) look like this, and Claude Code's status line then
    # carries rate_limits.seven_day and no five_hour at all.
    #
    # The frame says it rather than leaving pc/normalizer to infer it from
    # session_pct == UNKNOWN, because that value also means "the window
    # expired while nobody rendered", and the two want opposite answers: the
    # second borrows a session figure from the bus, the first must not, since
    # whatever it borrowed (a remembered payload, a Claude Desktop sample two
    # days old) describes a window the account does not have. No single
    # provider can tell them apart: pc/session_verdict weighs the evidence
    # every claude source reports (window_evidence, below) and marks the
    # frames before they are merged, on positive evidence only.
    # pc/normalizer.merge owns what the decision means; on a merged frame
    # the field is True exactly when merge() reported the session unknown
    # because of it, which is what the burn view (pc/ingest) keys on.
    no_session_window: bool = False
    # What this frame's source proves about the account's five-hour window,
    # or None when it proves nothing: (has_window, kind, at), built by
    # pc/session_verdict.evidence, `at` being when the figure it rests on
    # was taken. See pc/session_verdict for each kind and why quiet data is
    # never one.
    window_evidence: object = None

    def __post_init__(self):
        if self.active_at is None:
            self.active_at = self.observed_at
        if (self.first_seen_at is None or self.observed_at is None
                or self.first_seen_at > self.observed_at):
            self.first_seen_at = self.observed_at

    def n_sessions(self) -> int:
        return self.n_run + self.n_wait + self.n_stuck + self.n_idle

    def has_usage(self) -> bool:
        """True when this frame carries at least one usage percentage.

        A frame with neither window is not worthless -- it may still carry a
        model name or an execution state -- but it must not be allowed to win
        a recency contest for numbers it does not have. The normalizer merges
        field by field for exactly this reason.
        """
        return self.session_pct >= 0 or self.weekly_pct >= 0


# How long a five-hour window lasts, and the slack on any judgement made
# from it. ONE spelling: pc/normalizer places a stampless reading against a
# stamped one with SESSION_WINDOW_S, and two sources retire a session figure
# by session_outlived() below. Claude Desktop's retire used to spell
# 5 * 3600 + 600 out on its own beside the normalizer's 5 * 3600 (review,
# 2026-10-31).
#
# Ten minutes of slack, because the clocks a window is judged by are not the
# server's: Claude Desktop samples every five minutes, so a sample can be up
# to that late for the moment it describes, and Claude Code re-renders a
# second after a reset and on its own idle cadence after that. Ten minutes
# covers both with room; a different five-hour window is hours away.
SESSION_WINDOW_S = 5 * 3600
SESSION_SLACK_S = 600


def session_outlived(fetched_at, now_epoch: float) -> bool:
    """Has a five-hour window that was open when this reading was fetched
    certainly ended by now?

    For a reading with no reset stamp to say so itself. Claude Desktop's
    usage cache has none at all, and a status-line five_hour can carry one
    that will not read (null, a string, milliseconds): without a bound, such
    a figure stayed on the bus for as long as its file did, and on
    2026-10-30 a Desktop sample 37 hours old was still filling the session
    dial of an account that no longer had a window. Measured from when the
    figure was FETCHED, not from when its file was last written: Claude Code
    repaints an unchanged block with a new mtime every time it renders, so
    an mtime would never let it age out.
    """
    return now_epoch - fetched_at > SESSION_WINDOW_S + SESSION_SLACK_S


def window_ended(resets_at, now_epoch: float) -> bool:
    """Has the clock reached the reset time this reading carries?

    Asked separately from age because age cannot answer it: a reading
    taken a minute before the reset is accurate for that minute and wrong
    from the instant the window empties, however young the file still is.

    `resets_at` comes from the vendor's clock and `now_epoch` from ours, and
    nothing here tries to reconcile them. The mistake that leaves is exactly
    the skew -- N seconds fast ends a window N seconds early, N slow ends it
    N late -- which on a time-synced machine is a fraction of a second
    against windows of five hours and seven days. Measuring the skew would
    take a second vendor timestamp, and no source that calls this has one.
    """
    if resets_at is None:
        return False
    return now_epoch >= resets_at


class SettledWindow(typing.NamedTuple):
    """One window after settle_window(): what to show, the countdown still
    worth sending, and when the window emptied if it already has."""
    pct: float
    resets_at: object
    ended_at: object


def settle_window(pct: float, resets_at, now_epoch: float,
                  stale: bool) -> SettledWindow:
    """Bring one window's reading up to date with its own reset time.

    A window whose reset has passed is empty, by definition, so the honest
    reading is 0% and not a stale flag on the old number. The two sources
    that need this are both written only as a side effect of someone working
    -- Claude Code's status line when it repaints, a Codex rollout while
    Codex runs -- so nothing refreshes them overnight. Before this, a window
    that rolled at 03:00 kept the panel amber until its owner came back at
    09:00, and on 2026-08-22 a five-hour reset took a perfectly good weekly
    figure down with it. DevOceanIT saw the same shape on Codex, 2026-10-01:
    98% held after the window's resets_at had gone by four minutes, with
    nobody running Codex, until a session was resumed; that is why this sits
    in base and not in statusline_source, where it first lived.

    The three fields are answered one at a time, because the defect of
    2026-09-02 came from answering them together. An old payload used to
    skip this entirely, on the grounds that zeroing a three-day-old file
    invents a quiet three days. True, but skipping kept the PRE-RESET figure,
    which is certainly wrong, and dropped the evidence that the window had
    ended; a Claude Desktop sample from before the same reset then won the
    dial with stale=False. Crossing STALE_AFTER_S turned a correct 0% into a
    confident green 78% of usage already forgiven.

      ended_at   The reset epoch, whenever it has passed, stale or not. When
                 a window ended does not change as the file ages, and
                 pc/normalizer needs it to turn away a newer reading from a
                 source that cannot see reset times; nothing else in the
                 frame records it, since observed_at may predate the reset.
      resets_at  None once passed. protocol.secs_until rejects a past stamp
                 anyway, and merge() treats any reset time as "a source has
                 one", which mutes Claude Desktop's burn rate.
      pct        0.0 if the reading is fresh, UNKNOWN if it is stale (or was
                 already unknown). A stale reading cannot vouch for the time
                 since the reset, and the old figure is known to be wrong,
                 so neither is offered -- the same -1 statusline_source
                 gives a window that is missing.

    So a reading can come out of this with no number in either window. It
    still goes on the bus: pc/normalizer.merge sends such a provider as
    unknown and stale instead of leaving the panel on the pre-reset value.
    """
    if not window_ended(resets_at, now_epoch):
        return SettledWindow(pct, resets_at, None)
    # Written as the negation on purpose: a NaN percentage is neither < 0
    # nor >= 0, and a fresh NaN has always settled to 0.0, not UNKNOWN.
    vouched = not (stale or pct < 0)
    return SettledWindow(0.0 if vouched else UNKNOWN, None, resets_at)


class ProviderParser:
    """What a provider must implement to join the ingestion bus.

    The three methods below are the interface named in the handoff document.
    `poll` is the fourth thing a real provider needs and the document leaves
    implicit: something has to decide WHICH files to read and when, and that
    knowledge belongs to the provider rather than to the daemon loop. The
    daemon calls poll(); everything platform-specific stays behind it.

    Parsers must not raise. A provider whose source has changed shape upstream
    is expected to return None and let the bus fall back to another source --
    a daemon that dies because an app it does not control shipped a new cache
    layout is the failure this whole structure exists to avoid.
    """

    def get_provider_id(self) -> str:
        raise NotImplementedError

    def parse_cli_event(self, raw_payload, now_epoch, observed_at):
        """A push from the tool's own CLI. None when unusable."""
        return None

    def parse_cache_file(self, file_content, now_epoch, observed_at):
        """The tool's internal cache, already read off disk. None when unusable."""
        return None

    def poll(self, now_epoch):
        """Every frame this provider can produce right now, freshest first.

        Returns a list so a provider with three sources reports three
        readings and lets the normalizer arbitrate, rather than picking a
        winner with less information than the normalizer has.
        """
        return []
