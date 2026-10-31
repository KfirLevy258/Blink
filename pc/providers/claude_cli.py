"""Claude Code's status line, as a provider on the ingestion bus.

A thin adapter, on purpose. All of the reasoning about this source -- the
freshness bound and why age is the wrong primary signal, why an absent
window is -1 and not 0, and where the file is -- lives in
pc/statusline_source and stays there; carrying a window across its own
reset is pc/providers/base.settle_window, shared with Codex. Re-implementing
any of it here would give the project two rules for the same file.

This briefly read one payload per session, to take the worst of several
context windows and name the model in use. Both came off the panel -- with
several agents running there are several contexts at different levels and no
single number is any of them -- and what is left is account-wide: every open
terminal reports the same two percentages, so the most recent render is as
good as any.

Two payloads are remembered, each for a question the file in front of us
cannot answer.

The first is the last payload whose five-hour window we could actually
read. The file is rewritten when Claude Code renders, and when the window
expires with nobody rendering, the rewrite drops the percentage -- so the
reading does not lose a comparison, it stops existing, and the panel falls
to whatever else is on the bus. In the field that was a Claude Desktop
sample 57 hours old, whose age was drawn over a machine that had used
Claude Code six hours earlier.

The second is the last payload with ANY figure in it, offered when the
file in front of us has none: a session that has just started or been
resumed writes a status line with no rate_limits at all, and a file caught
mid-write does not read. On an account with no five-hour window
(pc/session_verdict) the first memory is empty or long ended, so without this
one such a render left the weekly figure, and with it the name and age of
the whole frame, to a Claude Desktop sample days old: the "37 hours old"
line came back on every such render and went away on the next (review,
2026-10-31).

The RAW payload is kept, not the frame it maps to, and it is re-mapped at
the current clock every time it is offered. That is the whole reason this is
five lines instead of a policy: staleness, a window carried across its own
reset, and the refusal to zero an old one all live in map_statusline_frame
and all depend on `now`. A cached frame would freeze `stale=False` at
capture and put a green dot over an hours-old number, which is precisely
what pc/normalizer's docstring exists to prevent.

The memories die with the process, deliberately. They answer "since this
daemon started, when did you last use Claude Code", and a daemon restart is
almost always an app update or a login -- after which the desktop cache is
as good an answer as we have. Persisting them would put a second copy of a
file that already exists on disk into ~/.blink, with its own invalidation
and corruption paths, for a case the field report does not contain. What
a fresh process cannot know about the account's five-hour window is
persisted, but by pc/session_verdict, as a verdict and not as a payload.

What this provider proves about that window goes on its frames as
window_evidence, for pc/session_verdict to weigh. A payload with a
five-hour window proves one, dated no later than the start of that window
(_window_dated). A payload without one proves nothing on its own: that is
also every Pro and Max status line from a reset to the next reply. Proof of
NO window is two replies of one session that came back without one, far
enough apart that no reset can explain them (_reply_without_window).

There is one more memory, and it answers a different question: not which
payload to fall back on, but how long ago the figures in the current one
were fetched. An mtime cannot say. Each repaint of the status line puts a
new mtime on the same rate_limits block, and when the merge ranked by mtime
a repainted 39 beat a Claude Desktop 40 sampled after it -- 39, 40, 39, 41
on the panel (docs/open-bugs/usage-merge-jitter.md). So each distinct block
is keyed to the oldest mtime it has appeared under, the frame carries that
as first_seen_at, and observed_at is left as the mtime.

It is not persisted either, for the reason above. A daemon that has just
started has never seen any block before, so the first poll dates each one
by its file's mtime; the cost is one poll of the old ranking, once per
restart.
"""
import json
import math

from pc import session_verdict
from pc import statusline_source as ss
from pc.providers import base

PROVIDER_ID = ss.PROVIDER_ID
SRC_ID = ss.SRC_ID

# The cap on the fetch-time memory, in blocks.
#
# Every new figure is a new key: a five-hour window that climbs from 0 to
# 100 a tenth at a time is a thousand keys, and a daemon runs for weeks. Yet
# a lookup only ever hits the payload in the file now or the one remembered
# for the session dial, so nearly all of the history is dead weight. 256
# keeps a generous margin over the two that matter; at under 200 bytes of
# JSON per key the whole table stays below about 50 KB.
FETCH_MEMORY_LIMIT = 256

# The cap on the reply baselines, in sessions. One entry per Claude Code
# session this process has seen render weekly-only; a desk runs a handful at
# once, and the oldest goes first.
REPLY_MEMORY_LIMIT = 64

# What a reply without a window has to look like before it is proof. See
# _reply_without_window for the argument; the three numbers are its inputs.
#
# REPLY_GAP_S: the longest two renders of one session may be apart for the
# cost between them to count as a reply. Claude Code renders every few
# seconds while it is open (docs/research/claude-ultra-no-session-window.md),
# so a longer silence is time nobody was watching -- the board unplugged
# (polls run only while it is alive), a provider cooldown, the daemon down
# -- and a whole window can open and close inside it. That is finding 2 of
# the /code-review max of 2026-11-01: a 09:00 baseline at 3.00, five hours
# of unpolled replies with five_hour, and a 14:05 weekly-only render at
# 4.00 read as a reply.
REPLY_GAP_S = 600
# REPLY_STREAM_S: the longest one API request can stream. Claude Code gives
# up on a request after API_TIMEOUT_MS, ten minutes unless the user raises
# it; five more minutes of margin. A request accepted before a reset and
# finished after it is the other half of the problem (finding 1).
REPLY_STREAM_S = 900
# REPLY_APART_S and REPLY_WITHIN_S: how far apart the two replies that make
# a proof must be. Far enough that one reset cannot explain both, and close
# enough that two consecutive resets, five hours apart, cannot either.
REPLY_APART_S = REPLY_GAP_S + REPLY_STREAM_S
REPLY_WITHIN_S = base.SESSION_WINDOW_S - REPLY_APART_S


def _block_key(payload):
    """(key, ends_at) for the payload's rate_limits block.

    `key` is the block as JSON with sorted keys, so two writes of the same
    figures compare equal whatever order the shim wrote them in. `ends_at`
    is the earliest reset stamp in the block, or None if it has none. Both
    are None when the payload has no usable block: there is nothing to
    remember, and the caller dates the frame by its mtime.
    """
    limits = payload.get("rate_limits") if isinstance(payload, dict) else None
    if not isinstance(limits, dict) or not limits:
        return None, None
    try:
        key = json.dumps(limits, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return None, None
    # The first moment this block describes a window that has ended: after
    # it, Claude Code's next fetch is certain to be a different block, so an
    # ended entry is the cheapest one to forget.
    ends = []
    for w in limits.values():
        r = w.get("resets_at") if isinstance(w, dict) else None
        if isinstance(r, (int, float)) and not isinstance(r, bool):
            ends.append(float(r))
    return key, (min(ends) if ends else None)


class ClaudeCliProvider(base.ProviderParser):
    def __init__(self, path=None):
        # None means "wherever ss.payload_path() says", asked on every poll
        # rather than once here. The default used to be a module constant in
        # statusline_source, expanduser'd at import before tests/conftest.py
        # redirects HOME, so every test that built the default bus read the
        # developer's REAL ~/.blink/statusline.json -- found when `status
        # --wire` under the sandbox put a live Claude reading on the wire
        # (2026-10-19). Resolving at construction instead closed that and left
        # the same hole one step later: the bus is built once and lives as
        # long as the daemon, so its path was fixed by whatever HOME was when
        # it was made (found in review). CLAUDE.md names this hole.
        self._path = path
        # (payload, mtime) for the last reading whose frame put a figure on
        # the session dial when it was read, or None before the first one.
        # The frame's, not the payload's: a five_hour percentage the frame
        # refused -- one whose resets_at will not read, fetched more than a
        # window ago (_frame) -- is not remembered. See the module docstring.
        self._remembered = None
        # (payload, mtime) for the last reading whose payload has any usable
        # figure, or None. When that reading also became _remembered, poll()
        # stored the same tuple object in both, and it compares them by
        # identity so as not to offer one payload twice. A reading with a
        # weekly figure and no session one is this and not _remembered, and
        # so is one whose five_hour figure the frame refused.
        self._any = None
        # {session_id: (cost.total_cost_usd or None, mtime, first reply's
        # mtime or None)} of that session's last weekly-only payload: the
        # baseline a reply is recognised against, and the reply a second
        # one is measured from. See _reply_without_window and
        # REPLY_MEMORY_LIMIT.
        self._weekly_only = {}
        # {block key: [oldest mtime it appeared under, epoch its first
        # window ends]}. See the module docstring and FETCH_MEMORY_LIMIT.
        self._fetch_times = {}

    def _note_fetch(self, payload, mtime, now_epoch):
        """Record this sighting of the payload's block; return its fetch time.

        The fetch time is the oldest mtime the block has appeared under, so
        a repaint never moves it forward. It CAN move back: if the file now
        carries an mtime older than the one on record, that mtime wins. A
        wound-back clock or a file restored from backup does that, and the
        alternative -- a fetch time later than the file's own stamp --
        would claim the figures were fetched after they were written down.
        """
        key, ends_at = _block_key(payload)
        if key is None:
            return mtime
        known = self._fetch_times.get(key)
        if known is None:
            self._make_room(now_epoch)
            self._fetch_times[key] = [mtime, ends_at]
            return mtime
        if mtime < known[0]:
            known[0] = mtime
        return known[0]

    def _make_room(self, now_epoch):
        """Shrink the table below FETCH_MEMORY_LIMIT before an insert.

        Blocks whose window has ended go first, all of them, since Claude
        Code's next fetch cannot reproduce them; only if that frees nothing
        does the block with the oldest fetch time go. The key about to be
        inserted cannot be dropped, but one poll can make two lookups (the
        file's payload, then the remembered one), and the second can drop
        the first's entry if that block's window has ended. That costs
        little: an ended window already reads 0 or unknown
        (base.settle_window), and the block re-enters on the next poll dated
        by its mtime, which is how every block was dated before this memory.
        """
        if len(self._fetch_times) < FETCH_MEMORY_LIMIT:
            return
        for key in [k for k, (_, ends_at) in self._fetch_times.items()
                    if ends_at is not None and ends_at <= now_epoch]:
            del self._fetch_times[key]
        if len(self._fetch_times) >= FETCH_MEMORY_LIMIT:
            del self._fetch_times[min(self._fetch_times.items(),
                                      key=lambda kv: kv[1][0])[0]]

    def get_provider_id(self) -> str:
        return PROVIDER_ID

    def path(self):
        return self._path if self._path is not None else ss.payload_path()

    def parse_cli_event(self, raw_payload, now_epoch, observed_at):
        """A push from Claude Code, already read.

        `observed_at` is the payload's mtime rather than now: the file is
        written when Claude Code renders, so its age is the reading's age.
        Passing now() here would make every reading look current, which is
        the one thing the freshness logic exists to prevent.
        """
        if not isinstance(raw_payload, dict):
            return None
        return ss.map_statusline_frame(raw_payload, now_epoch, observed_at)

    def _reply_without_window(self, payload, mtime):
        """Evidence of no five-hour window, when this payload is a reply
        that came back without one; otherwise None.

        On Pro and Max every API response carries the five-hour headers, so
        a Claude Code reply brings five_hour back into the status line. A
        payload rendered after a reply that still has none is proof the
        account has no window: verified Ultra, from 2026-10-30. Anything
        short of a reply proves nothing, because a weekly-only status line is
        also every Pro and Max desk from a reset to its next reply -- Claude
        Code drops an expired five_hour and re-renders a second after the
        reset (read from Claude Code 2.1.316's bundle).

        A reply is recognised in the status line itself, per session: this
        session's previous payload was weekly-only too, and since then its
        cost.total_cost_usd went up, which only this session's own API
        responses do. Not the seven_day used_percentage, though it moves off
        the same headers: it is the account's figure, not the session's, and
        in a generated day of three terminals in which it moved for all of
        them at once, a test on it marked a Pro desk on 6.4% of polls, where
        cost alone marked none (2026-10-31). Per session, because the file is shared by
        every Claude Code process on the desk: a terminal left open repaints
        its own old block over a reply another terminal just rendered
        (finding 10), and with no comparison across sessions that repaint
        proves nothing. Not the transcripts: their timestamps say a reply
        happened somewhere on the desk, not that the payload in this file
        came after it, and the burn reader that has them only runs once the
        verdict is in.

        ONE REPLY IS NOT PROOF; two far enough apart are. The /code-review
        max of 2026-11-01 found two ways a Pro or Max desk produced "a
        reply without a window", and either one saved a no-window verdict
        that outlived a restart -- the burn view on a desk that has a
        session gauge, which is the worst thing this module can do:

          - finding 1, a request accepted before a reset and still
            streaming after it. Claude Code re-renders a second after the
            reset without the expired five_hour; when the stream ends it
            credits the cost and applies headers whose five_hour resets at
            that same moment, which the render filter drops again. Cost
            rose between two weekly-only renders. Parallel subagents in
            flight across the reset do it several times;
          - finding 2, a baseline from before a gap in the polling. Polls
            run only while the board is alive, and a session that replied
            all morning with five_hour while nobody polled, then re-rendered
            weekly-only when the window ended, had risen since its 09:00
            baseline.

        So a reply counts only against a render of the same session at
        most REPLY_GAP_S before it (finding 2: an older baseline is a new
        baseline, never a comparison). On Pro or Max such a reply, rendered
        weekly-only at t, can only be a request that was accepted inside a
        window and finished after its reset R: accepted after its previous
        render less REPLY_STREAM_S, so R lies in (t - REPLY_APART_S, t].
        Two replies at least REPLY_APART_S apart then need two different
        resets, and consecutive resets are at least a window apart, so two
        replies also less than REPLY_WITHIN_S apart cannot both happen on
        Pro or Max. On Ultra a working session makes them within half an
        hour, and the verdict is kept from then on.

        Every five_hour seen in the file, from any session, starts every
        session's count again: the account has a window, whatever the
        arithmetic says. And so does this session's cost falling, which is
        not one counter going down but two processes on one session_id
        (`claude --continue` twice): their idle repaints alternate, and
        every other one was a rise (finding 1's second trigger).

        Dated by the payload's mtime, the render after the second reply.
        Its rate_limits block may be one the fetch-time memory has seen
        before -- the weekly figure need not move -- so first_seen_at would
        date it to before the reply.
        """
        limits = _limits(payload)
        if ss.window_present(limits, "five_hour"):
            # A window: every session's next weekly-only payload starts a
            # new count rather than being compared across it.
            self._weekly_only.clear()
            return None
        sid = payload.get("session_id") if isinstance(payload, dict) else None
        if not isinstance(sid, str) or not sid:
            return None
        if not ss.window_usable(limits, "seven_day"):
            return None
        cost = _number(_cost(payload))
        before = self._weekly_only.pop(sid, None)
        first = None
        proof = False
        if before is not None and cost is not None and before[0] is not None:
            was, seen, first = before
            if not 0 <= mtime - seen <= REPLY_GAP_S or cost < was:
                first = None                # unwatched, or two processes
            elif cost > was:                # a reply
                if first is not None and mtime - first >= REPLY_WITHIN_S:
                    first = None
                if first is None:
                    first = mtime
                else:
                    proof = mtime - first >= REPLY_APART_S
        self._weekly_only[sid] = (cost, mtime, first)   # newest last
        while len(self._weekly_only) > REPLY_MEMORY_LIMIT:
            del self._weekly_only[next(iter(self._weekly_only))]
        if not proof:
            return None
        return session_verdict.evidence(
            False, session_verdict.KIND_STATUSLINE_REPLY, mtime)

    def _frame(self, payload, mtime, now_epoch):
        """The frame this payload maps to now, dated, bounded, and carrying
        the evidence its payload alone gives.

        One path for every frame poll() offers, the remembered ones
        included, so a payload is judged the same way whichever memory it
        came out of.
        """
        frame = self.parse_cli_event(payload, now_epoch, mtime)
        if frame is None:
            return None
        frame.first_seen_at = self._note_fetch(payload, mtime, now_epoch)
        if (frame.session_pct >= 0 and frame.session_resets_at is None
                and frame.session_rolled_at is None
                and base.session_outlived(frame.first_seen_at, now_epoch)):
            # A five_hour whose resets_at will not read (null, a string,
            # milliseconds: statusline_source._window drops all three) never
            # ends by settle_window, so the remembered payload lent its
            # figure to the dial for as long as the process ran, with an age
            # that only grew (review, 2026-10-31). Bounded the way Claude
            # Desktop's stampless samples are: five hours and ten minutes
            # after it was fetched, a window open then has ended.
            frame.session_pct = base.UNKNOWN
        limits = _limits(payload)
        if ss.window_present(limits, "five_hour"):
            frame.window_evidence = session_verdict.evidence(
                True, session_verdict.KIND_STATUSLINE_WINDOW,
                _window_dated(limits, frame.first_seen_at))
        return frame

    def poll(self, now_epoch):
        payload, mtime = ss.read_payload(self.path())
        frames = []
        has_figure = False
        if payload is not None:
            # Asked of the live payload only, and on every read of it: the
            # baseline it keeps is this file's history, not a memory's.
            reply = self._reply_without_window(payload, mtime)
            live = self._frame(payload, mtime, now_epoch)
            if live is not None:
                if reply is not None:
                    live.window_evidence = reply
                frames.append(live)
                limits = _limits(payload)
                has_figure = (ss.window_usable(limits, "five_hour")
                              or ss.window_usable(limits, "seven_day"))
                seen = (payload, mtime)
                if has_figure:
                    self._any = seen
                # base.UNKNOWN is -1.0; anything >= 0 is a real percentage,
                # including the hard 0.0 map_statusline_frame computes for a
                # window it watched roll over.
                if live.session_pct >= 0:
                    self._remembered = seen
                    return frames

        # The live reading has no session figure, so offer the last one that
        # did -- ALONGSIDE the live frame rather than instead of it. They are
        # merged field by field, so the live payload keeps whatever it still
        # has (a seven-day percentage outlives the five-hour window every
        # time) and the remembered one is a candidate for the session dial
        # only. The normalizer then ranks it by recency like anything else,
        # so it wins the dial only when it is genuinely the freshest session
        # reading in the set. Where the account is marked as having no
        # window (pc/session_verdict), merge() refuses it the dial whatever
        # its age.
        if self._remembered is not None:
            remembered = self._frame(*self._remembered, now_epoch)
            if remembered is not None:
                frames.append(remembered)

        # And when the file in front of us has no figure at all, or did not
        # read, the last payload that had one: a render without rate_limits
        # must not hand the frame's name and age to whatever else is on the
        # bus.
        # See the module docstring.
        if (not has_figure and self._any is not None
                and self._any is not self._remembered):
            last = self._frame(*self._any, now_epoch)
            if last is not None:
                frames.append(last)
        return frames


def _window_dated(limits, first_seen_at):
    """When a five_hour block proves the account had a window.

    No later than the start of the window it describes, its resets_at less
    a window, when that stamp reads. first_seen_at, the date this used to
    carry, is this process's first sighting of the block, and it is only
    the fetch time if the process was watching when the block arrived. A
    restarted daemon, or one that first saw a terminal left open repaint a
    block it fetched hours ago, dated that block by the repaint: on an Ultra
    desk a block fetched before the change, repainted after a reply had
    proved no window, overturned the saved verdict (the /code-review max of
    2026-11-01, finding 4). The window's start does not depend on who was
    watching, and it is the moment the account certainly had a window.

    Earlier is the safe side here. The only evidence it now loses to is a
    no-window proof from inside the window itself, and on Pro or Max there
    is none such (_reply_without_window); a change of plan to Ultra is the
    one real case, and there the proof is right. A change back opens
    windows that start after it. The min() with first_seen_at also keeps a
    stamp in the wrong unit, or from a broken clock, from dating a window
    in the future, where it would outrank every proof for as long as the
    clock took to catch up.
    """
    resets_at = ss._window(limits, "five_hour")[1]
    if resets_at is None:
        return first_seen_at
    return min(first_seen_at, resets_at - base.SESSION_WINDOW_S)


def _limits(payload):
    """The payload's rate_limits object, or None."""
    limits = payload.get("rate_limits") if isinstance(payload, dict) else None
    return limits if isinstance(limits, dict) else None


def _cost(payload):
    """The session's cost.total_cost_usd, unchecked; None when absent."""
    cost = payload.get("cost") if isinstance(payload, dict) else None
    return cost.get("total_cost_usd") if isinstance(cost, dict) else None


def _number(v):
    """A finite number as a float, else None: never a bool, NaN or inf."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if math.isfinite(v) else None
