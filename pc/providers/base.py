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
    # exists. Only a source that can see reset timestamps can ever set it --
    # the CLI status line -- and pc/normalizer uses it to stop an older
    # reading from another source outliving the reset it never knew about.
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

    def __post_init__(self):
        if self.active_at is None:
            self.active_at = self.observed_at

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
