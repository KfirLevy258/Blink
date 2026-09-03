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
already spent code on (see pc/statusline_source._rolled_over).
"""
from pc.providers import base


def _known_pct(v):
    return v is not None and v >= 0


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


def _rolled_at(frames, attr):
    """The latest observed rollover for this window, or None.

    Evidence, not inference: some source watched the window empty at this
    epoch. Only the CLI status line can see it (pc/statusline_source), and it
    used to discard the fact after acting on it.
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

    session_pct, session_src = _pick(
        frames,
        lambda f: (_known_pct(f.session_pct)
                   and _survives_rollover(f, s_rolled, "session_rolled_at")),
        lambda f: f.session_pct)
    weekly_pct, weekly_src = _pick(
        frames,
        lambda f: (_known_pct(f.weekly_pct)
                   and _survives_rollover(f, w_rolled, "weekly_rolled_at")),
        lambda f: f.weekly_pct)

    if session_src is None and weekly_src is None:
        # Nothing here carries a usage percentage. A frame with only a model
        # name would render as two blank dials, which is worse than the
        # board keeping the last reading it already has.
        return None

    session_resets_at, _ = _pick(
        frames, lambda f: f.session_resets_at is not None,
        lambda f: f.session_resets_at)
    weekly_resets_at, _ = _pick(
        frames, lambda f: f.weekly_resets_at is not None,
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
    if session_resets_at is None:
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
    primary = session_src or weekly_src

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
    the primary, applied twice: preference first, then recency. A third
    provider is dropped rather than rotated, because a ring that silently
    changes whose number it is showing is worse than one that never shows it.
    """
    merged = [m for m in (merge(g) for g in group_by_provider(frames).values())
              if m is not None]
    if not merged:
        return None, None
    merged.sort(key=lambda m: (m.provider != preferred, -m.observed_at))
    return merged[0], (merged[1] if len(merged) > 1 else None)


def select(frames, preferred=None):
    """The single frame to put on the board, or None.

    The panel has two dials and one of everything else, so with more than one
    provider reporting, something has to choose. The rule:

      1. The preferred provider, if it has anything to say at all. A user who
         has said "show me Claude" does not want the display silently handed
         to another tool because that tool happened to write a file more
         recently.
      2. Otherwise the provider with the freshest reading.

    This is deliberately a choice between providers rather than a blend. What
    a two-provider panel SHOULD look like is a hardware design question and
    not one the normalizer gets to answer by averaging.
    """
    merged = [m for m in (merge(g) for g in group_by_provider(frames).values())
              if m is not None]
    if not merged:
        return None
    if preferred:
        for m in merged:
            if m.provider == preferred:
                return m
    return max(merged, key=lambda m: m.observed_at)
