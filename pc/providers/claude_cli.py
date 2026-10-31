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

One thing IS remembered, and only one: the last payload whose five-hour
window we could actually read. The file is rewritten when Claude Code
renders, and when the window expires with nobody rendering, the rewrite
drops the percentage -- so the reading does not lose a comparison, it stops
existing, and the panel falls to whatever else is on the bus. In the field
that was a Claude Desktop sample 57 hours old, whose age was drawn over a
machine that had used Claude Code six hours earlier.

The RAW payload is kept, not the frame it maps to, and it is re-mapped at
the current clock every time it is offered. That is the whole reason this is
five lines instead of a policy: staleness, a window carried across its own
reset, and the refusal to zero an old one all live in map_statusline_frame
and all depend on `now`. A cached frame would freeze `stale=False` at
capture and put a green dot over an hours-old number, which is precisely
what pc/normalizer's docstring exists to prevent.

The memory dies with the process, deliberately. It answers "since this
daemon started, when did you last use Claude Code", and a daemon restart is
almost always an app update or a login -- after which the desktop cache is
as good an answer as we have. Persisting it would put a second copy of a
file that already exists on disk into ~/.blink, with its own invalidation
and corruption paths, for a case the field report does not contain.

There is a second memory, and it answers a different question: not which
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
        # (payload, mtime) for the last reading that had a five-hour
        # percentage, or None before the first one. See the module docstring.
        self._remembered = None
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

    def _lacks_session_window(self, payload, live, now_epoch):
        """Does this fresh payload say the account has no five-hour window?

        Verified Ultra accounts (2026-10-30) have none, and Claude Code's
        status line then carries rate_limits.seven_day and no five_hour key
        while it renders every few seconds. Measured on such a desk,
        `blink status --wire` printed session_pct 1.0, src desktop, stale
        true, age_s 136136 one second after the file was written: the dial
        was filled by a Desktop sample two days old, and that borrowed
        source then named and dated the whole frame.

        Four conditions, each there because a real shape would otherwise be
        misread:

          - The payload is FRESH. A file nobody has rendered in half an hour
            says nothing about what the account has now.
          - The five_hour KEY is absent. Present without used_percentage is
            the likeliest shape of a payload change (statusline_source.
            _window), and not something to conclude "no window" from.
          - A weekly figure is usable. Without one there is nothing to put in
            the dial's place and the frame keeps its old behaviour.
          - The remembered five-hour window, if there is one, has ENDED. This
            is the discriminator, and it is read from the data: a window
            whose resets_at is still ahead is proof the account has one, so a
            render that leaves it out (Claude Code reports a window only
            after a session's first response) must not blank a live dial. An
            unstamped remembered window cannot be shown to have ended and
            counts the same way. Real payloads always carry the stamp, so
            this costs an unverified-then-verified account at most five
            hours of its old window ageing out.

        What this cannot tell apart is the other shape that ALSO has no
        five_hour key: a Pro or Max window that expired while nobody
        rendered, seen on the first render afterwards, with the previous
        window already ended in memory or no memory at all (a fresh daemon,
        or `blink status`, which is a fresh process). It is the same payload
        byte for byte. The rule treats it as Ultra for as long as the key
        stays away -- a "--%" session dial instead of 0% or a Desktop sample
        -- and the next payload that carries five_hour (a session's first
        response, normally within minutes of someone working) puts everything
        back. The other choice shows Ultra owners a borrowed number from a
        window they do not have, indefinitely, and that is what shipped.
        """
        if live.stale or live.weekly_pct < 0:
            return False
        limits = payload.get("rate_limits")
        if not isinstance(limits, dict) or "five_hour" in limits:
            return False
        if self._remembered is None:
            return True
        old = self._remembered[0].get("rate_limits")
        _, resets_at = ss._window(old if isinstance(old, dict) else {},
                                  "five_hour")
        return base.window_ended(resets_at, now_epoch)

    def poll(self, now_epoch):
        payload, mtime = ss.read_payload(self.path())
        frames = []
        if payload is not None:
            live = self.parse_cli_event(payload, now_epoch, mtime)
            if live is not None:
                live.first_seen_at = self._note_fetch(payload, mtime,
                                                      now_epoch)
                live.no_session_window = self._lacks_session_window(
                    payload, live, now_epoch)
                frames.append(live)
                # base.UNKNOWN is -1.0; anything >= 0 is a real percentage,
                # including the hard 0.0 map_statusline_frame computes for a
                # window it watched roll over.
                if live.session_pct >= 0:
                    self._remembered = (payload, mtime)
                    return frames

        # The live reading has no session figure, so offer the last one that
        # did -- ALONGSIDE the live frame rather than instead of it. They are
        # merged field by field, so the live payload keeps whatever it still
        # has (a seven-day percentage outlives the five-hour window every
        # time) and the remembered one is a candidate for the session dial
        # only. The normalizer then ranks it by recency like anything else,
        # so it wins the dial only when it is genuinely the freshest session
        # reading in the set.
        if self._remembered is not None:
            payload, mtime = self._remembered
            remembered = self.parse_cli_event(payload, now_epoch, mtime)
            if remembered is not None:
                remembered.first_seen_at = self._note_fetch(payload, mtime,
                                                            now_epoch)
                frames.append(remembered)
        return frames
