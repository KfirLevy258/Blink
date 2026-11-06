"""Replay a scenario timeline instead of reading real tools.

The fleet test suite runs the real daemon against real boards on several
machines, and every one of them needs to reproduce the same made-up usage
history -- "percentage climbs past 100", "this reading is four hours old" --
identically. A real provider reads whatever a tool happened to write on that
particular machine at that particular moment, which is exactly the opposite
of a repeatable scenario. This module trades the real sources for a JSON
script: the scenario becomes the source of truth instead of the machine, so
the same file drives the same board behaviour everywhere.

Scenario JSON format:

    {
      "name": "overage",
      "steps": [
        {"at": 0,  "provider": "claude", "session_pct": 97.0, "state": "running"},
        {"at": 10, "provider": "claude", "session_pct": 102.0, "state": "failed", "age_s": 3600}
      ]
    }

Each step is a NormalizedUsageFrame expressed as plain JSON, plus two fields
that are not part of the frame itself:

  - `at`: seconds since the provider was constructed. poll() emits a step
    once elapsed real time (per the `now` clock) reaches it. One NEW step per
    poll, oldest first: a poll turns into a single usage message however many
    frames it carries, so a late first poll has to delay the timeline rather
    than collapse the steps that came due while it waited. Between steps the
    last one is reported again, because that is what a source is -- a level
    that holds until something writes over it, not an event that happens
    once. See poll().

  - `age_s` (default 0): how old the reading should claim to be, i.e.
    `observed_at = now - age_s`. This exists because staleness and "how old
    is this" captions are themselves scenarios under test, not just an
    artifact of when the script happens to run -- a step can claim to be
    four hours old the instant it fires.

  - `burn` (optional): the five-hour burn view's twenty buckets while this
    step is on show (docs/plans/burn-5h.md) -- a name from BURN_PRESETS, or
    a list of twenty output-token counts, oldest first. It stands in for
    pc/burn_reader, which would otherwise read this machine's real
    transcripts into a scenario that is meant to be the same on every desk,
    and it is the bench's way to put each state on a board without spending
    anybody's quota (CLAUDE.md). It holds like the frame does until a later
    step says otherwise, and `"burn": null` clears it. It only reaches the
    board while the step also says `"no_session_window": true`, as on a
    real desk: the line is for an account with no session window.
    tests/fleet/bench/burn_states.json walks through every preset.

A step may also give a reset as `session_resets_in_s` / `weekly_resets_in_s`,
seconds after the reading, and the spend page in the plan message's own
names: `plan`, `usd`, `lim`, `period`, `acct`, `est` and `hint` (see
protocol.plan). `est` is the wire's sense, the inverse of the frame's
spend_exact, and like the frame it defaults to an estimate: a scripted
spend step is exact only when it says `"est": false`.

  - `sburn` (optional): the dollar burn view's twenty quarter-hours, in
    CENTS, while the step is on show -- a name from SBURN_PRESETS or twenty
    counts, held and cleared (`"sburn": null`) the way `burn` is. It stands
    in for pc/spend_count, the running count a desk builds from Claude
    Code's status line, so a bench walks the dollar view without anybody
    spending anything. The bus asks it only while the page is a spend page,
    as it asks the real count.
"""
import json
import math
import sys
import time

from pc import protocol
from pc.providers.base import NormalizedUsageFrame, ProviderParser

# Bucket sets for the burn view, in output tokens, oldest first: one for each
# thing the board draws differently. They go on the wire as they are, since
# the line carries exact tokens (protocol.burn). The board's direction
# compares complete quarter-hours only: the last hour is buckets 15-18 and
# the hour before it 11-14, and bucket 19, the quarter-hour still filling, is
# drawn but not compared. Both zero is idle, otherwise more than +10% is
# rising and less than -10% easing (docs/plans/burn-5h.md).
BURN_PRESETS = {
    # Nothing in five hours: "0", an en dash, no bars.
    "idle": [0] * 20,
    # The last hour within a point of the one before (+0.8%).
    "steady": [84_000, 92_000, 88_000, 95_000, 90_000, 86_000, 93_000,
               89_000, 91_000, 87_000, 94_000, 90_000, 88_000, 92_000,
               90_000, 91_000, 89_000, 93_000, 90_000, 90_000],
    # Quiet early, then nearly four times the hour before.
    "rising": [0, 0, 0, 0, 4_000, 9_000, 6_000, 12_000, 18_000, 15_000,
               22_000, 30_000, 26_000, 34_000, 41_000, 38_000, 95_000,
               140_000, 210_000, 265_000],
    # A busy hour tailing off to half of itself, and the quarter-hour in
    # progress down to a trickle.
    "easing": [40_000, 55_000, 70_000, 62_000, 80_000, 96_000, 110_000,
               120_000, 135_000, 150_000, 160_000, 170_000, 240_000,
               260_000, 230_000, 210_000, 120_000, 70_000, 35_000, 12_000],
    # One quarter-hour dwarfs the rest: every other bar sits on the board's
    # two-pixel floor for a non-zero bucket, and the total is in millions.
    "spike": [2_000] * 9 + [1_600_000] + [2_000] * 10,
    # The widest total there is to format: about a million a bucket.
    "heavy": [999_000 - 2_500 * i for i in range(20)],
}


# The dollar burn view's bucket sets, in cents a quarter-hour, oldest first:
# the same shapes as BURN_PRESETS, at the size a usage-billed seat spends.
# protocol.spend_burn clamps each to SBURN_MAX_CENTS.
SBURN_PRESETS = {
    # Nothing in five hours.
    "idle": [0] * 20,
    # About $1.20 an hour, the last hour within a few cents of the one
    # before.
    "steady": [30, 28, 31, 29, 30, 32, 27, 30, 31, 29, 30, 30, 28, 31, 30,
               29, 31, 30, 30, 30],
    # Quiet early, then several dollars a quarter-hour.
    "rising": [0, 0, 0, 0, 12, 25, 18, 40, 55, 48, 70, 95, 80, 110, 130,
               120, 310, 450, 680, 860],
    # A busy hour tailing off.
    "easing": [120, 160, 210, 190, 240, 290, 330, 360, 400, 450, 480, 510,
               720, 780, 690, 630, 360, 210, 105, 36],
    # The widest the panel has to format: thousands of dollars a bucket.
    "heavy": [999_900 - 2_500 * i for i in range(20)],
}


# Step keys that name a frame field by its wire name (protocol.plan) rather
# than its frame name. `plan` needs no entry: it is both. `est` is not here
# either: it is spend_exact's inverse, turned round in _build_frame.
_SHORT_NAMES = (("usd", "spend_usd"), ("lim", "spend_limit_usd"),
                ("period", "spend_period"), ("acct", "account"),
                ("hint", "spend_hint"))

# Step keys holding a countdown, and the frame field each becomes. A scenario
# is written once and replayed on any day, so it cannot name an epoch; it
# names how far past the reading the window ends. The spend timeline is the
# one that cannot do without it: a monthly period's end is weeks out, and the
# panel's countdown for it is half of what that page shows.
_COUNTDOWNS = {"session_resets_in_s": "session_resets_at",
               "weekly_resets_in_s": "weekly_resets_at"}


def _rebase_countdowns(fields, observed_at):
    """Turn every countdown key in `fields` into its absolute field, in
    place. A countdown that is not a number is dropped, as an absent reset
    is: the frame's own default (no reset known) is the honest reading."""
    for key in [k for k in fields if k in _COUNTDOWNS]:
        secs = fields.pop(key)
        if _is_number(secs):
            fields[_COUNTDOWNS[key]] = observed_at + secs


def _is_number(v):
    # bool is an int subclass; a step should never mean "at": true.
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _burn_of(value, presets=None, what="burn"):
    """A step's `burn` as twenty token counts (or its `sburn` as twenty
    cents, with SBURN_PRESETS); None for an explicit null.

    Raises ValueError for anything else, which the caller reports and skips:
    a typo in a hand-written scenario must not take the daemon down (see
    _build_frame).
    """
    if value is None:
        return None
    presets = BURN_PRESETS if presets is None else presets
    if isinstance(value, str):
        if value not in presets:
            raise ValueError(f"unknown {what} preset; expected one of "
                             + ", ".join(sorted(presets)))
        return list(presets[value])
    # isfinite: a scenario is JSON, and Python's json reads the bare literal
    # Infinity, which passed `v >= 0` and then raised OverflowError out of
    # int(), uncaught, taking the provider's poll down with it (review,
    # 2026-10-31). NaN already failed `v >= 0`; this says so for both. An
    # int is finite by definition and is not asked: isfinite() converts it
    # to a float first, and one past a float's range raised OverflowError,
    # which the caller does not catch either (/code-review max,
    # 2026-11-01). protocol.burn caps it at BURN_MAX like any other count.
    if (isinstance(value, list) and len(value) == protocol.BURN_BUCKETS
            and all(_is_number(v)
                    and (isinstance(v, int) or math.isfinite(v)) and v >= 0
                    for v in value)):
        return [int(v) for v in value]
    raise ValueError(f"{what} is a preset name or twenty finite counts of"
                     " zero or more")


class ScriptedBurn:
    """pc/burn_reader.BurnReader's stand-in under BLINK_SCENARIO.

    The same poll(now) -> (q, tokens) | None, answered from the step the
    provider has on show rather than from transcripts. q is the real
    quarter-hour, so the board labels the hours its own clock is in.
    """

    def __init__(self, provider):
        self._provider = provider

    def poll(self, now):
        tokens = self._provider.burn_tokens()
        if tokens is None:
            return None
        return protocol.burn_quarter(now), list(tokens)


class ScriptedSpendCount:
    """pc/spend_count.SpendCounter's stand-in under BLINK_SCENARIO, for the
    one thing the bus asks of it: buckets(now), the dollar burn view's
    twenty quarter-hours in cents. None while no step has set any, which
    the bus takes as no line (IngestionBus.spend_burn).

    Nothing is observed, bound or saved: a scenario's count is whatever its
    steps say, and a bench must not write ~/.blink/spend-count.json.
    """

    def __init__(self, provider):
        self._provider = provider

    def buckets(self, now):
        cents = self._provider.sburn_cents()
        return None if cents is None else list(cents)


class ScriptedProvider(ProviderParser):
    def __init__(self, path, now=time.time):
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        self.name = doc.get("name", "scenario")
        self._steps = sorted(self._usable_steps(doc.get("steps", [])),
                              key=lambda s: s["at"])
        self._emitted = set()
        # The most recent step's frame, re-reported until the next one is
        # due, the way a real source's current reading is. See poll().
        self._held = None
        # The burn buckets the steps so far have set, held the same way, and
        # the dollar burn view's.
        self._burn = None
        self._sburn = None
        self._now = now
        self._t0 = now()

    @staticmethod
    def _usable_steps(raw_steps):
        """Scenario steps shaped enough to sort and replay.

        A scenario file is hand-authored, and a typo in it -- a step with no
        `at`, or one that is not even an object -- must not take the
        provider down before it can construct at all. Same "return nothing
        usable" spirit as parse_cli_event; here that means dropping the one
        step rather than the whole scenario.
        """
        for i, step in enumerate(raw_steps):
            if not isinstance(step, dict) or not _is_number(step.get("at")):
                print(f"scripted: skipping step {i}, no numeric 'at'",
                      file=sys.stderr)
                continue
            yield step

    def get_provider_id(self) -> str:
        return "scripted"

    def poll(self, now_epoch):
        """The OLDEST step that has come due and not yet been emitted. One --
        and after that, that same reading again until the next step is due.

        Not every due step, which is what this used to return, because the
        daemon collapses one poll into one usage message: IngestionBus.poll
        hands its frames to normalizer.select_pair(), which picks a primary
        and a secondary and sends exactly one frame to the board. Two steps
        emitted together are therefore one frame on the wire, not two.

        And two steps do come due together on a healthy desk. The daemon's
        first poll is gated on board_alive() (claude_usage_bridge.py:1076-1084),
        false until the board's first ping; a scenario pass connects to a board
        that is already running, so there is no boot hello and the first ping
        lands up to PING_INTERVAL_MS (10 s) later -- and a poll skipped by that
        gate is lost rather than deferred, since next_poll advances either way.
        Meanwhile this provider stamps t0 at construction. Every fleet scenario
        writes min_tx as its own step count, so merging two steps into one
        frame fails a perfectly healthy board -- and a flaky release gate
        teaches people to re-run until green, which ends the gate.

        One per call turns a late start into a delayed timeline instead of a
        lost frame: each step gets its own poll, its own message, and its own
        [usage] line off the board. The scenarios have the headroom for it --
        the six-step usage_climb, polled every 3 s, delivers its last step
        around 27 s into a 44 s pass even if the first poll only fires at 12 s.

        A step whose frame cannot be built does not consume the call: it is a
        typo in a hand-written scenario, not a reading, and dropping the poll
        with it would cost the run a frame it was owed.

        THE STEP IS HELD AFTERWARDS, which is the half this was missing. A
        real provider reports a level: it returns the current reading on every
        poll until the source writes a new one. This returned each step once
        and then nothing, so whichever of the daemon's two ticks happened to
        consume a step decided whether the board ever saw it -- and the fast
        tick drops a frame whose only change is `age_s`, because age is
        deliberately volatile (protocol.VOLATILE_USAGE_KEYS: the board ages
        the reading against its own clock, so re-sending it is traffic
        carrying no news).

        That cost stale_age its last step on the first real fleet run: the
        fast tick took the 4-hour reading, correctly said "no news", and the
        3-second heartbeat -- which sends unconditionally and would have
        delivered it -- found the provider empty from then on. A healthy
        board and a correct daemon, failed by a provider that behaves like
        nothing that exists.

        The held frame keeps its ORIGINAL observed_at, so its age grows the
        way a real stale reading's does instead of being pinned.
        """
        elapsed = self._now() - self._t0
        for i, step in enumerate(self._steps):
            if i in self._emitted or step["at"] > elapsed:
                continue
            self._emitted.add(i)
            frame = self._build_frame(step)
            if frame is not None:
                self._held = frame
                self._take_burn(step, i)
                return [frame]
        return [self._held] if self._held is not None else []

    def _take_burn(self, step, i):
        """Hold this step's burn buckets, when it names any. A bad value
        keeps the ones before it, reported like a bad step."""
        if "burn" in step:
            try:
                self._burn = _burn_of(step["burn"])
            except ValueError as e:
                print(f"scripted: step {i} burn ignored ({e})",
                      file=sys.stderr)
        if "sburn" in step:
            try:
                self._sburn = _burn_of(step["sburn"], SBURN_PRESETS, "sburn")
            except ValueError as e:
                print(f"scripted: step {i} sburn ignored ({e})",
                      file=sys.stderr)

    def burn_tokens(self):
        """The twenty buckets on show, or None when no step has set any."""
        return None if self._burn is None else list(self._burn)

    def sburn_cents(self):
        """The dollar view's twenty buckets on show, or None."""
        return None if self._sburn is None else list(self._sburn)

    def spend_count(self):
        """What claude_usage_bridge.build_bus hands the bus in place of the
        running spend count."""
        return ScriptedSpendCount(self)

    def burn_source(self):
        """What claude_usage_bridge.build_bus hands the bus in place of the
        transcript reader."""
        return ScriptedBurn(self)

    def _build_frame(self, step):
        """One step as a frame, or None when the step doesn't fit.

        A field name that doesn't match NormalizedUsageFrame -- a typo, or a
        step that collides with `provider`/`src`/`observed_at` -- raises
        TypeError out of the constructor. This is the fleet-test analogue of
        the no-raise rule every other provider follows (base.py:154-157): a
        daemon on all three machines must not die because one scenario file
        has a typo in it. The step is dropped instead, same as an unusable
        real-source reading.
        """
        try:
            age = step.get("age_s", 0)
            observed_at = self._now() - age
            # `burn` is the stand-in reader's (see _take_burn), not a frame
            # field, and would otherwise drop the step as a typo.
            fields = {k: v for k, v in step.items()
                      if k not in ("at", "age_s", "provider", "burn",
                                   "sburn")}
            # The spend page in the wire's own short names, so a scenario
            # reads like the plan message it produces. Spelling one field
            # both ways is a typo, and lands as the TypeError below.
            for short, field in _SHORT_NAMES:
                if short in fields:
                    if field in fields:
                        raise TypeError(f"both {short!r} and {field!r}")
                    fields[field] = fields.pop(short)
            # The wire's "est" says the dollars are a guess; the frame's
            # spend_exact says the opposite. A bool only: "est": 1 from a
            # copied wire line would otherwise turn exact into a guess
            # silently, and a typo should be dropped, not obeyed.
            if "est" in fields:
                if "spend_exact" in fields:
                    raise TypeError("both 'est' and 'spend_exact'")
                est = fields.pop("est")
                if not isinstance(est, bool):
                    raise TypeError("'est' is true or false")
                fields["spend_exact"] = not est
            _rebase_countdowns(fields, observed_at)
            return NormalizedUsageFrame(
                provider=step.get("provider", "claude"),
                src="scripted",
                observed_at=observed_at,
                **fields)
        except TypeError as e:
            print(f"scripted: skipping malformed step ({e})", file=sys.stderr)
            return None
