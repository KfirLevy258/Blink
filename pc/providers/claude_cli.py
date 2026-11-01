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
five-hour window proves one, dated by its mtime: when it was rendered, so
any five_hour rendered after a no-window mark lifts it (_frame). A payload
without one proves nothing on its own: that is also every Pro and Max
status line from a reset to the next reply. Proof of NO window is Claude
Code replying for more than five hours with no five_hour rendered at all,
or, on an account whose Claude Code config names the Ultra plan
(pc/claude_plan), one reply with none (_reply_without_window).

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

from pc import claude_plan
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
# session this process has seen render; a desk runs a handful at once, and
# the oldest goes first.
REPLY_MEMORY_LIMIT = 64

# What proves no window, from the status line. See _reply_without_window for
# the argument; these are its inputs. Replaced on 2026-11-01, after the
# re-review of that day measured three premises of the two-reply rule
# false in Claude Code 2.1.316's bundle (read, not run): the status line
# renders on events -- token usage, a mode change, a new assistant message,
# the earliest reset, prompt-cache expiry at about an hour -- not every few
# seconds; API_TIMEOUT_MS bounds only the wait for headers, so a body can
# stream for half an hour and more; and five_hour leaves the line by the
# LOCAL clock (resets_at > Date.now()/1000), so on a desk whose clock runs
# fast it goes before the window does.
#
# CHAIN_SPAN_S: replies with no five_hour, the first and the latest more than
# a window and a quarter of an hour apart at the least.
CHAIN_SPAN_S = base.SESSION_WINDOW_S + 15 * 60
# CHAIN_MAX_GAP_S: the longest a chain may go from one weekly-only reply to
# the next; past it the chain starts again at the later one. Measured from
# the LATEST such reply, not from the last one a tally kept: until the 3rd
# review of 2026-11-01 (finding 2) replies within 25 minutes of the tallied
# one neither counted nor moved it, so a 36-minute silence broke a chain
# that the rule said an hour's would. Not a pause rule -- an Ultra desk that
# breaks for over an hour is marked later, which is the side to be wrong
# on -- but what keeps two resets from joining. On Pro or Max a weekly-only
# reply happens only around a reset, while a request accepted before it is
# still finishing after it, or in the last S of a window on a clock S fast,
# and the next such stretch starts most of a window later: five hours less
# S, the stream and BASELINE_MAX_S. Two such stretches on a desk that did
# not use Claude Code in between (the window opened from claude.ai), or
# whose use in between went unseen, would otherwise join into one chain.
#
# There was a CHAIN_REPLIES of three and a REPLY_SPACING_S of 25 minutes
# until that review. The span already implied more than three (the review
# counted seven), and once the gap was taken from the latest reply the
# spacing decided nothing but which replies the tally kept: the proof is
# the span and the gap.
CHAIN_MAX_GAP_S = 3600
# BASELINE_MAX_S: the oldest a session's previous render may be for a cost
# rise since then to count as a reply. A rise against an older one is not
# counted, the chain is left as it is, and the render is the new baseline.
# Not 600 s, which assumed renders every few seconds: a user reading for
# eleven minutes broke every proof (finding 2 of the re-review). Half an
# hour is a reading pause.
#
# It is also what makes a gap in the polling harmless to the safety (a long
# one still restarts the chain: see _reply_without_window). A rise says the
# session replied some time after its baseline; the render that shows it may
# be a repaint long after that reply, whose own render (a five_hour one, on
# Pro or Max) was never seen -- the board unplugged, the lid closed, the
# daemon restarting, another terminal writing over it within a poll.
# Bounding the baseline's age bounds how far after its window's reset a
# counted render can be: the stream plus half an hour. Without it, eight
# terminals that replied inside one unwatched window and repainted after
# its reset, fifty minutes apart, proved a Pro desk had no window
# (test_a_rise_against_a_baseline_older_than_the_bound_is_no_reply). Until
# the 3rd review a poll gap over five minutes threw the chain away as
# well; an Ultra desk then had to stay awake and plugged in for five and a
# quarter hours straight to be marked (its finding 1), and the safety never
# rested on it: see _reply_without_window.
BASELINE_MAX_S = 30 * 60


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
        # _number, not float(): an int too big for a float raised
        # OverflowError here, the first thing every poll() does, and the
        # source was benched (the re-review of 2026-11-01, finding 12).
        r = _number(w.get("resets_at")) if isinstance(w, dict) else None
        if r is not None:
            ends.append(r)
    return key, (min(ends) if ends else None)


class ClaudeCliProvider(base.ProviderParser):
    def __init__(self, path=None, plan_path=None):
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
        # {session_id: (cost.total_cost_usd or None, mtime)} of that
        # session's last render of any shape: the baseline a reply is
        # recognised against. See _reply_without_window and
        # REPLY_MEMORY_LIMIT.
        self._costs = {}
        # The replies without a window so far, for the whole desk: [first
        # reply's mtime, latest one's], or None. See _reply_without_window.
        # In memory only, so a restarted daemon starts the chain again.
        self._chain = None
        # The clock of the last poll(), to see it go back; None before the
        # first.
        self._polled_at = None
        # The mtime of the render the file held when the clock last went
        # back, which is no baseline however often it is read again; or
        # None. See _forget_replies. _stale_owed: the clock went back on a
        # poll whose read failed, and the next read that succeeds names it.
        self._stale = None
        self._stale_owed = False
        # Block keys _make_room may not drop during the current poll: the
        # payloads poll() is about to look up. See _make_room.
        self._keep = ()
        # {block key: [oldest mtime it appeared under, epoch its first
        # window ends]}. See the module docstring and FETCH_MEMORY_LIMIT.
        self._fetch_times = {}
        # The account's plan, from Claude Code's .claude.json, for the fast
        # path of _reply_without_window. `plan_path` None is
        # claude_plan.config_path(), asked at each look, for the reason
        # self._path is.
        self._plan = claude_plan.PlanReader(path=plan_path)

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
        does the block with the oldest fetch time go.

        Except the blocks this poll looks up (self._keep): the file's
        payload, the remembered one and the last with any figure, three
        lookups a poll. The remembered payload is very often a block whose
        window has ended -- it is offered precisely because the file has no
        session figure any more -- and when the table filled it was purged
        with the other ended blocks, then re-entered by the next lookup
        under the remembered mtime, a later one: the dial's figure was
        re-dated forward by up to a window (the /code-review max of
        2026-11-01, finding 8, which needs 256 distinct blocks). A kept key
        can leave the table over the limit by at most two entries.
        """
        if len(self._fetch_times) < FETCH_MEMORY_LIMIT:
            return
        keep = self._keep
        for key in [k for k, (_, ends_at) in self._fetch_times.items()
                    if ends_at is not None and ends_at <= now_epoch
                    and k not in keep]:
            del self._fetch_times[key]
        if len(self._fetch_times) >= FETCH_MEMORY_LIMIT:
            spare = [kv for kv in self._fetch_times.items()
                     if kv[0] not in keep]
            if spare:
                del self._fetch_times[min(spare, key=lambda kv: kv[1][0])[0]]

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

    def _forget_replies(self, mtime):
        """Start the proof over, the clock having gone back: no chain, and
        no baseline to count a rise against, so the next render of every
        session is a new baseline. `mtime` is the render the file holds now,
        written by the clock from before the step: it is not that next
        render, and is never a baseline (self._stale).

        Until the 3rd review of 2026-11-01 the baselines were cleared and
        nothing more, and the same poll then read the file and took its
        render straight back in as a baseline: the next reply rose against
        it with its age short by the step
        (test_a_clock_gone_back_leaves_no_baseline_before_it). Every other
        render from before the step is gone, written over in the file.

        `mtime` is None when that poll's read failed (an antivirus or
        backup hold on Windows, EMFILE), and the stale render is then the
        one the next successful read finds (self._stale_owed): taken as
        None, the next poll took the pre-step render back in as a baseline
        all the same (4th review, 2026-11-01). That read may instead find a
        render made after the step, which is then no baseline either: one
        reply later to be counted, the safe side.
        """
        self._chain = None
        self._costs.clear()
        self._stale = mtime
        self._stale_owed = mtime is None

    def _reply_without_window(self, payload, mtime, now_epoch=None):
        """Evidence of no five-hour window, when this payload completes the
        proof; otherwise None. `now_epoch` (the poll's clock; the mtime when
        None) only paces the plan's look (claude_plan.RECHECK_S).

        THE FAST PATH, on the Ultra plan (owner's request, 2026-11-01:
        Ultra desks switch on the first reply again). When Claude Code's
        .claude.json names the Ultra plan (pc/claude_plan: planDisplayName
        "Ultra", or with no planDisplayName a rate-limit tier ending in
        max_20x), ONE reply -- recognised exactly as below, cost risen
        against a baseline at most BASELINE_MAX_S old -- that rendered a
        usable seven_day and no five_hour is proof, kind
        KIND_STATUSLINE_ULTRA. The sustained rule below exists because one
        such reply happens on Pro and Max as well, around a reset; a plan
        that is not Ultra never takes this path, and neither does a desk
        whose file is missing, unreadable, corrupt, too big or of any other
        shape. Each of those keeps the sustained rule. The window evidence
        is unchanged: an Ultra account Anthropic has not yet verified still
        has a temporary five-hour limit, and its next reply renders
        five_hour, which is newer than the mark and lifts it (_frame). So
        on that account a reply streaming across a reset can mark it until
        its next reply, minutes later at work; that is what the fast path
        costs, and only on Ultra.

        THE RULE. Claude Code has replied with no five_hour in the status
        line, the first such reply and the latest at least CHAIN_SPAN_S
        apart (a window and a quarter of an hour), each no more than
        CHAIN_MAX_GAP_S after the one before, and no five_hour seen from any
        session in that time. The replies may come from any session on the
        desk; the account is one. A short gap in the polling does not break
        the chain: see BASELINE_MAX_S. One of about an hour, less two reply
        intervals, does: a reply rendered while nothing polls is never seen,
        and the first rise after the gap is against a baseline older than
        BASELINE_MAX_S and not counted, so the next counted reply is more
        than CHAIN_MAX_GAP_S after the last one before the gap (4th review,
        2026-11-01: unplugged 57 min, replies every two minutes, proved on
        time; 58 min, three hours late).

        WHY IT HOLDS ON PRO AND MAX. It rests on the replies themselves,
        not on seeing anything between them: every one the chain takes
        rendered seven_day with no five_hour. Every API response carries
        the five-hour headers, and Claude Code shows five_hour while its
        resets_at is ahead of the local clock. So on a plan with windows a
        reply renders weekly-only in only two ways: a request accepted
        before a reset and still streaming after it (the body is not
        bounded: API_TIMEOUT_MS covers only the headers), and, on a clock
        running fast by S, the last S of every window. A rise is a response
        no earlier than its baseline, at most BASELINE_MAX_S (B) before the
        render; so with L the longest stream, a counted render lies within
        S before a reset and L + B after it. Windows are five hours apart at
        the least, so those stretches are separated by 5 h - (S + L + B)
        or more. The chain needs every step within CHAIN_MAX_GAP_S and a
        span of CHAIN_SPAN_S: inside one stretch that takes S + L + B of
        5 h 15 min, and to join two it takes S + L + B of four hours. With
        S = 0 that is a stream of three and a half hours. A five_hour seen
        in between starts the chain over too (a desk that uses Claude Code
        mid-window shows one), but nothing above depends on seeing it, so a
        poll gap -- the board unplugged, the lid closed -- costs the safety
        nothing. On Ultra, where the fast path cannot read the plan, a desk
        at work makes the chain in five and a quarter hours, breaks in its use of up to an hour included and poll
        gaps of up to an hour less two reply intervals (above), and the
        verdict is kept from then on.

        THE RESIDUAL. A Pro or Max desk is still marked if its clock runs
        fast by five hours or more (it never renders five_hour at all), or
        by four and three-quarter hours less its longest stream (one
        stretch spans the chain), or by three and a half hours less its
        longest stream while its Claude Code use in the middle of windows
        goes unseen -- it uses Claude Code only late in windows opened
        elsewhere, or the board is unplugged whenever it does (two
        stretches an hour apart join). The half hour of B in the last two
        needs a reply whose own render was never seen and a later render
        that showed it; without that each is half an hour more (5 h 15 min
        and 4 h less the stream). Or if every five_hour it renders is
        overwritten by another terminal before a poll sees it, window after
        window, with the clock fast as above. None has been seen. Short of
        those, a desk is marked late, never early: slow is the safe side.
        A restarted daemon has lost the chain, which is in memory only, and
        a desk the fast path cannot vouch for is then marked five and a
        quarter hours later; on the Ultra plan, at its next reply.

        A REPLY is recognised in the status line itself, per session: this
        session's cost.total_cost_usd went up since its previous render,
        which only this session's own API responses do. Not the seven_day
        used_percentage, though it moves off the same headers: it is the
        account's figure, and in a generated day of three terminals in which
        it moved for all of them at once, a test on it marked a Pro desk on
        6.4% of polls, where cost alone marked none (2026-10-31). Per
        session, because the file is shared by every Claude Code process on
        the desk: a terminal left open repaints its own old block over a
        reply another terminal just rendered (finding 10 of the review of
        2026-10-31), and with no comparison across sessions that repaint
        proves nothing. Not the transcripts: their timestamps say a reply
        happened somewhere on the desk, not that the payload in this file
        came after it, and the burn reader that has them only runs once the
        verdict is in.

        What starts the chain over, for every session:
          - any five_hour in the file: the account has a window, whatever
            the arithmetic says;
          - a session's cost FALLING, which is not one counter going down
            but two processes on one session_id (`claude --continue`
            twice). Their renders alternate, and every other one is a rise
            that no reply made; resetting only that session's baseline let
            the rises through one at a time (the review of 2026-11-01,
            finding 1's second trigger);
          - an mtime going backwards, against the session's previous render
            or the chain's latest reply: a clock stepped back, or a file
            restored, and the intervals mean nothing;
          - the polls' own clock going backwards between two polls, after
            which the render the file holds is no baseline, nor any other
            session's earlier one (poll(), _forget_replies). A step back
            smaller than the time since the last poll -- the clock set right
            while the lid was closed -- is not seen there, and is caught
            only as an mtime going backwards, above, if one does; otherwise
            the render from before it reads as one by a clock that was fast,
            which THE RESIDUAL already prices.
        A rise against a baseline older than BASELINE_MAX_S is not counted
        and changes nothing else: not even CHAIN_MAX_GAP_S's measure, since
        the render may be long after the reply it shows.

        Dated by the payload's mtime, the render of the reply that completed
        the chain -- and every reply after it renews the evidence.
        """
        limits = _limits(payload)
        sid = payload.get("session_id") if isinstance(payload, dict) else None
        if not isinstance(sid, str) or not sid:
            sid = None
        cost = _number(_cost(payload))
        before = None
        if mtime == self._stale:
            sid = None                      # from before the clock went back
        if sid is not None:
            before = self._costs.pop(sid, None)
            self._costs[sid] = (cost, mtime)        # newest last, for the cap
            while len(self._costs) > REPLY_MEMORY_LIMIT:
                del self._costs[next(iter(self._costs))]
        if ss.window_present(limits, "five_hour"):
            self._chain = None
            return None
        if before is None or cost is None or before[0] is None:
            return None
        was, seen = before
        if mtime < seen or cost < was:
            self._chain = None              # a clock back, or two processes
            return None
        if cost == was:
            return None                     # a repaint
        if mtime - seen > BASELINE_MAX_S:
            return None                     # unwatched for too long
        if not ss.window_usable(limits, "seven_day"):
            return None
        chain = self._chain
        if chain is not None and (mtime < chain[1]
                                  or mtime - chain[1] > CHAIN_MAX_GAP_S):
            chain = None
        if chain is None:
            chain = [mtime, mtime]
        else:
            chain[1] = mtime
        self._chain = chain
        if chain[1] - chain[0] >= CHAIN_SPAN_S:
            # Said as the sustained proof even on Ultra: it does not rest on
            # a file Claude Code may rewrite, and `blink status` names it.
            return session_verdict.evidence(
                False, session_verdict.KIND_STATUSLINE_SUSTAINED, mtime)
        # Asked last, only of a reply that already qualifies, so a desk
        # with a window stats the file only around a reset.
        if self._plan.ultra(mtime if now_epoch is None else now_epoch):
            return session_verdict.evidence(
                False, session_verdict.KIND_STATUSLINE_ULTRA, mtime)
        return None

    def _frame(self, payload, mtime, now_epoch):
        """The frame this payload maps to now, dated, bounded, and carrying
        the evidence its payload alone gives.

        One path for every frame poll() offers, the remembered ones
        included, so a payload is judged the same way whichever memory it
        came out of.

        A five_hour block is evidence of a window dated by the payload's
        mtime: when it was RENDERED, not when its block was fetched or its
        window opened. So any five_hour rendered after a no-window mark
        lifts it at once, on a running daemon, a restarted one and a fresh
        `blink status` alike. Until the re-review of 2026-11-01 it was dated
        by the window's start unless this process had watched it arrive
        with a reply (_window_dated, _arrived), to keep an old terminal's
        repaint of a block fetched before an account became Ultra from
        overturning the proof. That machinery could hold a wrong mark over a
        Pro or Max dial for a window, and had faults of its own (the
        re-review's findings 5, 6 and 11). The repaint it guarded against
        now does lift an Ultra mark, for as long as it is the newest
        evidence: the dial shows that old figure, and a chain proves no
        window again five hours later. A mark lost is the safe direction.
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
        if ss.window_present(_limits(payload), "five_hour"):
            frame.window_evidence = session_verdict.evidence(
                True, session_verdict.KIND_STATUSLINE_WINDOW, mtime)
        return frame

    def poll(self, now_epoch):
        # A clock gone back: every interval measured across the step is
        # short by it, the unsafe direction for CHAIN_MAX_GAP_S, so the
        # proof starts over. Seen here only when this poll is dated before
        # the last one: a step back hidden inside a longer gap is left to
        # the mtime checks and the residual (_reply_without_window). A gap
        # going FORWARD is no reason to: until the 3rd review of 2026-11-01
        # (finding 1) any gap over five minutes threw the chain away, and
        # the daemon polls only while the board is alive, so an Ultra desk
        # whose lid closed for ten minutes was not marked that day. What a
        # gap can hide is BASELINE_MAX_S's business.
        back = self._polled_at is not None and now_epoch < self._polled_at
        self._polled_at = now_epoch
        payload, mtime = ss.read_payload(self.path())
        if back:
            self._forget_replies(mtime)
        elif self._stale_owed and mtime is not None:
            self._stale, self._stale_owed = mtime, False
        self._keep = tuple(
            _block_key(p)[0] for p in (
                payload, *(m[0] for m in (self._remembered, self._any)
                           if m is not None)))
        frames = []
        has_figure = False
        if payload is not None:
            # Asked of the live payload only, and on every read of it: the
            # baseline it keeps is this file's history, not a memory's.
            reply = self._reply_without_window(payload, mtime, now_epoch)
            live = self._frame(payload, mtime, now_epoch)
            if live is not None:
                if reply is not None:
                    live.window_evidence = reply
                frames.append(live)
                # The frame's figures, not the payload's: a five_hour the
                # frame refused (_frame) is no figure, and a payload with
                # nothing else in it used to replace the last one that had
                # one, weekly figure and all (the /code-review max of
                # 2026-11-01, finding 14).
                has_figure = live.has_usage()
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


def _limits(payload):
    """The payload's rate_limits object, or None."""
    limits = payload.get("rate_limits") if isinstance(payload, dict) else None
    return limits if isinstance(limits, dict) else None


def _cost(payload):
    """The session's cost.total_cost_usd, unchecked; None when absent."""
    cost = payload.get("cost") if isinstance(payload, dict) else None
    return cost.get("total_cost_usd") if isinstance(cost, dict) else None


def _number(v):
    """A finite number as a float, else None: never a bool, NaN or inf.

    Nor an int too big for a float. json reads integers of up to 4300
    digits, float() of one past 308 raises OverflowError, and a cost that
    size in the status line made every poll() raise, so the source was
    benched for as long as the file said it (the /code-review max of
    2026-11-01). Claude Code does not write one; a corrupt file can.
    """
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        v = float(v)
    except OverflowError:
        return None
    return v if math.isfinite(v) else None
