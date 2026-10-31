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
(_has_no_window) the first memory is empty or long ended, so without this
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
and corruption paths, for a case the field report does not contain. The
one thing that costs is named at _has_no_window.

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
        # The latest moment a payload this process read showed a five-hour
        # window to exist, or None if it has read none. A time, not a flag:
        # see _note_window, and _has_no_window for why the answer depends
        # on how long ago.
        self._window_seen_at = None
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

    def _note_window(self, payload, mtime):
        """Record that this payload carried a five-hour window, if it did.

        Recorded as the latest moment the window is known to have existed:
        its reset, when the payload says one, and the payload's mtime when
        that is later or there is none. The mtime alone was too early. A
        Pro or Max window can be read once when it opens and never again --
        nobody renders for five hours -- and its reset is then hours after
        the only sighting, so the weekly-only render a second after that
        reset read as "no window seen for six hours" and blanked a correct
        post-reset Claude Desktop figure (the reviewer's altitude probe,
        scenario G). A stamp is capped at one window past the mtime, since
        no window outlasts its own length, so a stamp far in the future
        cannot hold the rule off for the life of the process.
        """
        limits = _limits(payload)
        if not ss.window_present(limits, "five_hour"):
            return
        until = mtime
        resets = ss.window_resets_at(limits, "five_hour")
        if resets is not None:
            until = max(mtime, min(resets, mtime + base.SESSION_WINDOW_S))
        if self._window_seen_at is None or until > self._window_seen_at:
            self._window_seen_at = until

    def _has_no_window(self, payload, fetched_at):
        """Does this payload say the account has no five-hour window?

        Verified Ultra accounts (2026-10-30) have none, and Claude Code's
        status line then carries rate_limits.seven_day and no five_hour
        while it renders every few seconds. Measured on such a desk,
        `blink status --wire` printed session_pct 1.0, src desktop, stale
        true, age_s 136136 one second after the file was written: the dial
        was filled by a Desktop sample two days old, and that borrowed
        source then named and dated the whole frame.

        Yes when the payload has a usable seven_day, and no payload this
        provider has read showed a window existing within SESSION_WINDOW_S +
        SESSION_SLACK_S before the figures in this one were fetched
        (_note_window; base.session_outlived is that bound, the one Claude
        Desktop's samples and a stampless five_hour are retired by). Both
        halves are read off the payload and its own times, never the poll's
        clock: a file read an hour after it was written says what it said
        when it was written. The previous rule judged the weekly figure at
        the poll's clock and the window at the payload's (review,
        2026-10-31). A payload that carries a five-hour window
        (statusline_source.window_present, the one definition) is never
        marked, because it is its own sighting: poll() notes it before any
        frame is made of it, at no earlier than its mtime, and the fetch
        time is never later than the mtime. It used to be asked
        window_present a second time here as well (review, 2026-10-31).

        The last condition is what tells Ultra from the shape that looks
        exactly like it. Claude Code drops an expired five_hour and
        re-renders one second after the reset (read from Claude Code
        2.1.316's bundle), so on every Pro and Max desk, at every reset, the
        status line goes weekly-only. The previous rule held "no window" as
        a latch from that payload until one carried five_hour again, and a
        latch cannot tell the two: it held "--%" from the reset to the next
        Claude Code reply, where the desk had shown 0% or Claude Desktop's
        post-reset figure (review, 2026-10-31). A Pro or Max account carried
        a window within the last five hours -- the one that just reset --
        and an Ultra account never does, so the evidence decides.

        "Fetched" is first_seen_at, the oldest mtime this rate_limits block
        has appeared under, not the file's mtime. An idle Claude Code
        repaints the same post-reset block for as long as it stays open,
        each time with a new mtime, and judged by mtime a Pro or Max desk
        working in Claude Desktop with a terminal left open would go "no
        window" five hours and ten minutes after its last Claude Code
        reply. The block is the same fetch however often it is painted. The
        price is the other way round and paid once per account: on the day
        an Ultra account loses its window inside a running daemon, the mark
        waits for a weekly-only block fetched more than five hours after the
        last window was seen, which is the next time the weekly figure
        moves, or the next restart.

        What this cannot tell apart, and accepts: a process with no memory
        -- a restarted daemon, or `blink status`, which is a fresh process
        -- reading a Pro or Max file whose last render came after its
        window reset. It is the same payload byte for byte, and it is read
        as Ultra ("--%", the burn view) until Claude Code's next reply
        carries the new window. Claude Desktop's Local Storage lifts it
        sooner when it holds a window that is still open: a reset time ahead
        of the clock proves a window (normalizer.merge).
        """
        if not ss.window_usable(_limits(payload), "seven_day"):
            # Nothing to put in the dial's place, and no claim to make.
            return False
        seen = self._window_seen_at
        return seen is None or base.session_outlived(seen, fetched_at)

    def _frame(self, payload, mtime, now_epoch):
        """The frame this payload maps to now, dated, bounded and marked.

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
        frame.no_session_window = self._has_no_window(payload,
                                                      frame.first_seen_at)
        return frame

    def poll(self, now_epoch):
        payload, mtime = ss.read_payload(self.path())
        frames = []
        has_figure = False
        if payload is not None:
            self._note_window(payload, mtime)
            live = self._frame(payload, mtime, now_epoch)
            if live is not None:
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
        # reading in the set. Where a frame here is marked no-window, merge()
        # refuses it the dial whatever its age.
        if self._remembered is not None:
            remembered = self._frame(*self._remembered, now_epoch)
            if remembered is not None:
                frames.append(remembered)

        # And when the file in front of us has no figure at all, or did not
        # read, the last payload that had one, with its own mark: a render
        # without rate_limits must neither drop the account's "no window"
        # nor hand the frame's name and age to whatever else is on the bus.
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
