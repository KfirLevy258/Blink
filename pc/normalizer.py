"""The universal telemetry normalizer: many sources in, one frame out.

Sources disagree, and they disagree in a specific way that decides the rule
used here. Each one sees a different slice of the truth:

  - The CLI hook knows the reset timestamps, the context window and the model,
    and is rewritten only when Claude Code renders its status line.
  - The desktop cache knows two percentages, has NO reset timestamps at all,
    and is rewritten every five minutes while the app is open.
  - Neither can see usage from claude.ai or a phone.

So no source is authoritative for everything, and picking one winning source
per poll would throw away fields the loser was the only one holding. The merge
is therefore FIELD BY FIELD, and the rule for each field is strict recency:
among the sources that actually have this field, the one whose reading was
taken most recently wins.

A rule that was considered and rejected, because it is the tempting one:
"prefer the HIGHER percentage, since a source that cannot see claude.ai
understates the truth." That reasoning holds only WITHIN a window. Across a
reset it inverts, and badly -- a window that rolled over a minute ago reads 0%
from the fresh source and 90% from one taken before the reset, and
higher-wins would confidently show 90% for as long as the stale reading
survives. Understating a limit is a real cost; inventing usage that has
already been forgiven is a worse one, and it is the failure this project
already spent code on (see pc/providers/base.settle_window).

The two percentages are the exception to "strict recency", in two narrow
ways, and both came from one logged trace (docs/open-bugs/usage-merge-jitter.md).
Recency there means the newest FETCH (first_seen_at), not the newest file
write: the status line is rewritten on every redraw. And a fresh reading may
keep the dial against a newer one that is at most HOLD_BAND below it, because
within a window usage only ever rises and a fraction of a point between two
sources is a disagreement, not a fall. See _pick_pct.
"""
import math

from pc.providers import base

# How far below the dial a newer reading may land and still be taken for
# rounding rather than a fall.
#
# The widest disagreement the logged trace showed between the status line
# and Claude Desktop on one window was a whole point (cli 39, desktop 40), so
# the band is exactly that and no wider. Anything wider is drawn at once as
# the real change it probably is -- a reset to 0, or a fall of 2. A band that
# swallowed a genuine fall would be the frozen meter this project exists to
# avoid, so it errs narrow.
HOLD_BAND = 1.0

# Two reset stamps this close are the same window. Claude's are fixed per
# window; Codex derives its from a countdown and a timestamp, so it can drift
# by a second or two. A different five-hour window is hours away.
SAME_WINDOW_S = 300

# How long each window lasts, which is what lets a reading with no reset
# stamp be placed against one that has a stamp: see _same_window.
SESSION_WINDOW_S = 5 * 3600
WEEKLY_WINDOW_S = 7 * 86400


def _stamp(f, attr):
    """A frame's reset stamp, or None when it has none or it is not a finite
    number. inf - inf is NaN and fails every comparison, so two infinite stamps
    made _same_window false even for a frame against itself, and max() of no
    peers raised -- one corrupt "resets_at": 1e999 took every provider's
    usage down. Sources bound their own stamps (statusline_source._window);
    this keeps the merge safe from one that does not."""
    v = getattr(f, attr, None)
    return v if v is not None and math.isfinite(v) else None


def _known_pct(v):
    # Finite, because a hold compares readings by subtracting them and
    # inf - inf is NaN, which fails every comparison and left max() with
    # nothing to choose from: one infinite percentage took the whole poll
    # down, every provider's numbers with it. A reading that is not a number
    # is no reading, for that frame alone. Sources check their own range
    # (statusline_source._window); this keeps the merge safe from one that
    # does not.
    return v is not None and math.isfinite(v) and v >= 0


def _pick(frames, has, get):
    """The freshest frame that actually has this field, or None.

    `has` decides whether a frame carries the field at all; a frame that does
    not is not a candidate, which is the whole point -- the desktop cache
    must never win the reset-timestamp contest by being newest when it has no
    reset timestamp to offer.
    """
    best = None
    for f in frames:
        if not has(f):
            continue
        if best is None or f.observed_at > best.observed_at:
            best = f
    return get(best) if best is not None else None, best


def _same_window(a, b, resets_attr, window_s):
    """Could these two frames be readings of the same window?

    Both stamped: only if the stamps agree to within SAME_WINDOW_S.

    One stamped, one not (Claude Desktop's cache has no stamps): the stamp
    says when the current window opened, resets_at - window_s, and a
    stampless reading taken before that moment is of an earlier window.
    Without this the hold had a reset hole: Claude Code can fetch a new
    window without ever having seen the old one end, so no rollover is
    reported, and a Desktop sample of 0.8 taken a minute before the reset
    sat within a point of the status line's post-reset 0.3 and came back
    onto the dial (found in review). A stamp rounded to the hour puts the
    computed opening up to an hour early, and a sample taken in that hour
    before the real reset still passes, so where stamps are rounded this
    narrows the hole and does not close it. The rollover evidence in merge()
    is the rule that closes it, whenever any source watched the reset.

    Neither stamped: nothing to compare, so yes.
    """
    ra = _stamp(a, resets_attr)
    rb = _stamp(b, resets_attr)
    if ra is not None and rb is not None:
        return abs(ra - rb) <= SAME_WINDOW_S
    if ra is None and rb is None:
        return True
    stamped, bare = (a, b) if ra is not None else (b, a)
    opened = _stamp(stamped, resets_attr) - window_s
    return bare.observed_at >= opened


def _pick_pct(frames, has, attr, resets_attr, window_s):
    """(value to show, the frame that won, the frame to date it by).

    All three are None when no frame has this window. Three rules, each
    found the hard way:

      1. Fresh before stale, then the newest FETCH. Ranking by observed_at
         let a status-line redraw -- a new mtime over the same numbers --
         retake the dial from a Desktop sample taken after those numbers
         were fetched. A stale frame is still a candidate; it just loses to
         any fresh one, and among stale frames recency still rules, which
         is what pc/providers/claude_cli's payload memory relies on.
      2. The hold. When the winner is fresh, a fresh frame a little ABOVE it
         -- at most HOLD_BAND -- keeps the dial, so a rounding difference
         between two sources never draws as a step back. Never a stale
         frame, never a frame from another window, and never a frame `has`
         turned away: that predicate is where merge() puts the rollover
         rule, so a reading from before a reset cannot come back this way.
      3. The date. Whichever frame holds the number, the caption is dated
         by the freshest fresh frame whose value agrees with what is shown
         to within the band. Holding Desktop's 40 against a status line
         redrawn a second ago at 39 must not put "5 min ago" under the dial
         when the old merge drew a live one. While the number shown is one
         the old merge would also have accepted, the panel's age caption
         and stale mark can appear later because of this, never sooner. The
         one way they can appear sooner is rule 1 putting a DIFFERENT number
         up -- Desktop's 40 over a redrawn 38 -- and then the caption is
         that number's own age, which is the honest one.

    The hold ends when the held frame goes stale, and the dial then drops
    to the winner: a hold is a delay, not a promise. Desktop's 40 sampled
    at 30 s over a live status-line 39 reads 40 until the sample passes the
    half-hour bound, then 39 -- one step back, half an hour later, where the
    old merge drew one every time the two sources took turns.
    """
    candidates = [f for f in frames if has(f)]
    if not candidates:
        return None, None, None
    winner = min(candidates, key=lambda f: (f.stale, -f.first_seen_at,
                                            -f.observed_at))
    shown = getattr(winner, attr)
    if winner.stale:
        return shown, winner, winner

    # Fresh readings of the winner's own window. The winner is one of them.
    peers = [f for f in candidates
             if not f.stale
             and _same_window(f, winner, resets_attr, window_s)]

    # Rule 2: the highest peer whose excess over the winner fits the band.
    shown = max(getattr(f, attr) for f in peers
                if 0 <= getattr(f, attr) - shown <= HOLD_BAND)

    # Rule 3: of the peers that agree with `shown`, the most recently heard.
    dated_by = max((f for f in peers
                    if 0 <= shown - getattr(f, attr) <= HOLD_BAND),
                   key=lambda f: f.observed_at)
    return shown, winner, dated_by


def _rolled_at(frames, attr):
    """The latest observed rollover for this window, or None.

    Evidence, not inference: some source watched the window empty at this
    epoch. Only a source with reset timestamps can see it: the Claude status
    line (pc/statusline_source), Claude Desktop's Local Storage
    (pc/providers/claude_desktop_ls) and, since a Codex window was seen
    holding 98 past its own reset, the Codex rollout (pc/providers/codex_cli).
    The status line used to discard the fact after acting on it.

    Then it discarded it a second way, which cost the same bug twice: the
    fact was reported only while the payload carrying it was fresh, so half
    an hour after Claude Code went quiet this returned None again and a
    desktop sample taken before the reset walked back onto the dial. The
    producer now reports the epoch whatever age the reading has reached --
    a window ended when it ended -- so everything below can assume that a
    rollover, once seen, stays seen for as long as the source is on the bus.
    """
    best = None
    for f in frames:
        at = getattr(f, attr, None)
        if at is not None and (best is None or at > best):
            best = at
    return best


def _survives_rollover(f, rolled_at, attr):
    """May this frame's reading of that window still be believed?

    Two ways to qualify. Either it was taken at or after the reset, or the
    frame ACCOUNTED for the reset itself -- which the reporting frame does,
    and it must, because its own observed_at is the payload's mtime and can
    be long before the window rolled.
    """
    if rolled_at is None:
        return True
    return f.observed_at >= rolled_at or getattr(f, attr, None) is not None


def merge(frames):
    """One frame per provider from many source frames, or None.

    Frames from different providers are never merged into each other: two
    providers are two accounts with two separate limits, and averaging or
    overriding across them would produce a number that is true of neither.
    """
    frames = [f for f in frames if f is not None]
    if not frames:
        return None

    provider = frames[0].provider
    assert all(f.provider == provider for f in frames), \
        "merge() takes frames from one provider; group them first"

    # A reading taken before a window reset describes a window that no longer
    # exists, however fresh it looks.
    #
    # Strict recency alone got this wrong, and badly, in the one direction the
    # module docstring says the design exists to prevent. The status line is
    # rewritten only when Claude Code renders, so its post-reset 0% can easily
    # be OLDER than a desktop sample taken minutes before the same reset --
    # and the desktop cache cannot see reset times at all, so it has no way to
    # know its own reading has been superseded. Recency handed it the dial and
    # the panel showed a confident, un-stale 78% for usage that had already
    # been forgiven, with a burn rate attached. Reproduced by execution
    # 2026-08-28; found by review.
    s_rolled = _rolled_at(frames, "session_rolled_at")
    w_rolled = _rolled_at(frames, "weekly_rolled_at")

    # An account with no session window has no session figure to pick. The
    # frame that says so is fresh by construction (ClaudeCliProvider marks
    # only a fresh payload), and it is also the right frame to date and name
    # the reading by: it IS the live status line. Without this, a remembered
    # payload or a Claude Desktop sample two days old filled the dial, won
    # `primary` below, and the board was told live data was 37 hours old
    # (measured 2026-10-30, a verified Ultra account).
    no_window = [f for f in frames if f.no_session_window and not f.stale]
    if no_window:
        session_pct, session_src, session_dated = None, None, None
    else:
        session_pct, session_src, session_dated = _pick_pct(
            frames,
            lambda f: (_known_pct(f.session_pct)
                       and _survives_rollover(f, s_rolled,
                                              "session_rolled_at")),
            "session_pct", "session_resets_at", SESSION_WINDOW_S)
    weekly_pct, weekly_src, weekly_dated = _pick_pct(
        frames,
        lambda f: (_known_pct(f.weekly_pct)
                   and _survives_rollover(f, w_rolled, "weekly_rolled_at")),
        "weekly_pct", "weekly_resets_at", WEEKLY_WINDOW_S)

    # Set when no percentage survives but a window was seen to end: the
    # provider then goes out as unknown rather than not at all, dated and
    # judged by the newest frame that saw a window end.
    ended_evidence = None
    if session_src is None and weekly_src is None:
        # Nothing here carries a usage percentage. A frame with only a model
        # name would render as two blank dials, which is worse than the
        # board keeping the last reading it already has.
        #
        # Unless the reason there is no percentage is that every window the
        # sources knew about has ENDED. Then the last reading the board has
        # is the pre-reset one, and keeping it is the failure: a Codex
        # window that rolled over with Codex closed held 98 on the panel
        # (DevOceanIT's desk, 2026-10-01), and returning None here put it
        # back the moment the reading aged past stale -- for Codex and, on
        # the same path, for a Claude status line gone quiet across both
        # resets. Two
        # blank dials are the truth in that case, and they keep the
        # provider's place on the panel and its state. Stale only if the
        # newest witness is: a Desktop Local Storage record that saw the
        # reset a minute ago is not an old reading. Blank does not mean
        # first, either -- select() ranks a provider with numbers above it.
        evidence = [f for f in frames
                    if f.session_rolled_at is not None
                    or f.weekly_rolled_at is not None]
        if not evidence:
            return None
        ended_evidence = max(evidence, key=lambda f: f.observed_at)

    # Nor a countdown or a rate for it: a remembered window's reset stamp
    # would otherwise ride along on a dial that is drawn "--%".
    if no_window:
        session_resets_at = None
    else:
        session_resets_at, _ = _pick(
            frames, lambda f: _stamp(f, "session_resets_at") is not None,
            lambda f: f.session_resets_at)
    weekly_resets_at, _ = _pick(
        frames, lambda f: _stamp(f, "weekly_resets_at") is not None,
        lambda f: f.weekly_resets_at)
    state, state_src = _pick(
        frames, lambda f: f.state != base.STATE_UNKNOWN, lambda f: f.state)

    # The burn rate, and the rule that keeps it from ever competing with a
    # real countdown.
    #
    # It is carried ONLY when no source could supply a session reset time. A
    # measured rate is a poor substitute for the server's own answer and a
    # good substitute for nothing at all, so the moment Claude Code reports a
    # reset the rate stops being sent -- no precedence contest, no state to
    # unwind, and one invariant the panel can rely on: a frame never carries
    # both.
    if no_window:
        session_burn_pph = None
    elif session_resets_at is None:
        # Same rule for the rate: a slope measured before the window emptied
        # describes the old window. It is the strongest case for the rule --
        # a reset leaves session_resets_at None, which is precisely the state
        # that lets a rate through at all.
        session_burn_pph, _ = _pick(
            frames,
            lambda f: (f.session_burn_pph is not None
                       and _survives_rollover(f, s_rolled, "session_rolled_at")),
            lambda f: f.session_burn_pph)
    else:
        session_burn_pph = None

    # The primary dial decides two things that have to agree with each other.
    #
    # `src` names where the session percentage came from, because that is the
    # number the largest dial shows -- reporting anything else would put a
    # label on the panel that does not describe the figure next to it. And
    # `stale` is that same source's staleness, not the whole set's: a fresh
    # desktop reading merged with an hours-old CLI reading is not a stale
    # panel, it is a live percentage next to a reset time that has simply
    # stopped being updated (and secs_until already refuses a past one).
    #
    # Since the hold, the frame that names and dates the dial is not always
    # the frame that won it: it is the freshest one whose figure agrees with
    # the number drawn to within HOLD_BAND (_pick_pct, rule 3). Still a
    # source that vouches for the figure next to it, which is the point of
    # the paragraph above, and never a stale one while a fresh one agrees.
    if no_window:
        primary = max(no_window, key=lambda f: f.observed_at)
    else:
        primary = session_dated or weekly_dated or ended_evidence

    # And the fact `primary` cannot carry: when this provider last said
    # anything at all.
    #
    # Every frame in the group counts here, including the ones that won
    # nothing -- a status line rewritten five seconds ago without a five-hour
    # percentage lost the dial to a remembered reading from this morning, and
    # it is still proof that Claude Code is running and somebody is at the
    # desk. `observed_at` above answers "how old is the number"; this answers
    # "how long since we heard a voice", and the board sleeps on the second
    # question (see pc/providers/base and protocol.usage's `active_age_s`).
    #
    # Taken from active_at rather than observed_at so merging a merged frame
    # keeps the freshest contact instead of narrowing it to the winner's.
    active_at = max(f.active_at for f in frames)

    return base.NormalizedUsageFrame(
        provider=provider,
        src=primary.src,
        observed_at=primary.observed_at,
        first_seen_at=primary.first_seen_at,
        active_at=active_at,
        session_pct=session_pct if session_pct is not None else base.UNKNOWN,
        session_resets_at=session_resets_at,
        weekly_pct=weekly_pct if weekly_pct is not None else base.UNKNOWN,
        weekly_resets_at=weekly_resets_at,
        state=state or base.STATE_UNKNOWN,
        stale=primary.stale,
        session_burn_pph=session_burn_pph,
        # Carried so a merged frame is still a valid input to another merge.
        session_rolled_at=s_rolled,
        weekly_rolled_at=w_rolled,
        no_session_window=bool(no_window),
        # The counts travel with the state they describe. Taking them
        # field-by-field like everything else would let a session count from
        # one source sit beside a state from another, which is a panel saying
        # "3 sessions, one of them stuck" on evidence that never agreed.
        n_run=state_src.n_run if state_src else 0,
        n_wait=state_src.n_wait if state_src else 0,
        n_stuck=state_src.n_stuck if state_src else 0,
        n_idle=state_src.n_idle if state_src else 0,
        n_agents=state_src.n_agents if state_src else 0,
        label=state_src.label if state_src else "",
    )


def group_by_provider(frames):
    """{provider_id: [frame, ...]}, preserving order within each provider."""
    out = {}
    for f in frames:
        if f is not None:
            out.setdefault(f.provider, []).append(f)
    return out


def select_pair(frames, preferred=None):
    """(primary, secondary) -- what the outer and inner rings should show.

    The panel has two rings per gauge and no more, so beyond two providers
    something has to be left off. The rule is the same one select() uses for
    the primary, applied twice: a provider with a number first, then
    preference, then recency. A third provider is dropped rather than
    rotated, because a ring that silently changes whose number it is showing
    is worse than one that never shows it.

    A provider with no number is never the secondary. merge() sends one whose
    windows have all ended so the board stops showing its pre-reset figure,
    and that is reason enough to own the MAIN dial when nothing else has a
    number. It is no reason for a second page: any Codex desk that ever ran
    Claude Code keeps an old statusline.json whose rollover evidence never
    expires, and it would carry a blank Claude inner ring forever, where
    before that evidence reached the wire there was no second page at all.
    """
    merged = [m for m in (merge(g) for g in group_by_provider(frames).values())
              if m is not None]
    if not merged:
        return None, None
    merged.sort(key=_rank(preferred))
    secondary = merged[1] if len(merged) > 1 else None
    if secondary is not None and not secondary.has_usage():
        secondary = None
    return merged[0], secondary


def select(frames, preferred=None):
    """The single frame to put on the board, or None.

    The panel has two dials and one of everything else, so with more than one
    provider reporting, something has to choose. The rule:

      1. A provider with a number before one without. merge() sends a
         provider whose every window has ended as unknown, so that it is not
         dropped -- it is not thereby entitled to the main dial.
      2. The preferred provider. A user who has said "show me Claude" does
         not want the display silently handed to another tool because that
         tool happened to write a file more recently.
      3. Otherwise the provider with the freshest reading.

    This is deliberately a choice between providers rather than a blend. What
    a two-provider panel SHOULD look like is a hardware design question and
    not one the normalizer gets to answer by averaging.
    """
    merged = [m for m in (merge(g) for g in group_by_provider(frames).values())
              if m is not None]
    if not merged:
        return None
    return min(merged, key=_rank(preferred))


def _rank(preferred):
    """Sort key for merged frames, best first: see select().

    Numbers before preference, and that order is the point. `preferred` is
    always "claude" on a real desk (pc/ingest; the firmware announces it),
    and an old statusline.json whose resets have both passed is rollover
    evidence that never expires -- so ranked on preference alone, a Claude
    Code last used weeks ago put two blank dials on the main page every day
    and pushed a Codex in daily use, 55/33, to the inner ring. Before blank
    providers reached the wire at all, Codex had that dial (found in review).
    """
    return lambda m: (not m.has_usage(), m.provider != preferred,
                      -m.observed_at)
