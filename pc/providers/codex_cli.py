"""Codex CLI's rollout log, as a second provider on the ingestion bus.

Same shape as every other source here: read a figure another program has
already worked out, from a file it writes for its own reasons. Nothing in
this module authenticates to anyone, and it never reads a prompt, a reply or
a tool call -- only the `rate_limits` object Codex records alongside its
token counts.

Where the numbers are
---------------------
Codex appends one JSON object per line to

    ~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<stamp>-<uuid>.jsonl

and after a turn completes it writes an `event_msg` whose payload type is
`token_count`. That payload carries `rate_limits`:

    "rate_limits": {
      "primary":   {"used_percent": 0.0, "window_minutes": 300,   "resets_at": ...},
      "secondary": {"used_percent": 0.0, "window_minutes": 10080, "resets_at": ...}
    }

300 minutes is the five-hour window and 10080 is the seven-day one, which is
exactly the pair the panel draws.

Three things about that file are worth stating here, because each is a way to
be confidently wrong rather than to fail:

  - `primary` and `secondary` are POSITIONS, not windows. This matches them by
    `window_minutes` and only falls back to the position when the field is
    missing, for the same reason claude_desktop range-checks its percentages:
    a rename upstream should cost us the source, not swap the two dials.

  - `resets_at` is an absolute epoch in SECONDS, unlike Claude Desktop's
    sample timestamps, which are milliseconds. Verified against a real file,
    2026-08-27; the sanity bound below is what would catch it changing.

  - Codex compresses a rollout to `.jsonl.zst` once it is a week old
    (codex-rs/rollout/src/compression.rs, MIN_ROLLOUT_AGE). The glob below
    sees only plain `.jsonl`, which is right for as long as that floor is
    days: the freshest reading is always in a live, plain file, and a file
    old enough to be compressed is a week past being stale anyway.
    tests/ci/check_codex_contract.sh fails the day the floor drops.

  - The newest file is not always the newest reading. A terminal left open
    with no turn in it produces a rollout file with no `token_count` at all,
    and its mtime still moves. So this reads the few most recently touched
    files and takes the freshest event out of all of them, rather than
    trusting the first one it finds.
"""
import glob
import json
import os
from datetime import datetime, timezone

from pc.providers import base

PROVIDER_ID = "codex"
SRC_ID = "cli"

# Same bound, and for the same reason, as the other two file-backed sources:
# Codex only writes this while it is running, so age here measures how long
# ago you last used Codex, not how wrong the figure is.
STALE_AFTER_S = 1800

# Which window each entry describes, by its own declared length. The tolerance
# is generous because these are round numbers chosen by a product, not
# measurements: anything under a day is the short window, anything longer is
# the long one.
SESSION_WINDOW_MAX_MIN = 60 * 24

# Plausible bounds for a reset timestamp: 2020-01-01 to 2100-01-01. What this
# would notice is the unit changing to milliseconds.
RESET_EPOCH_MIN = 1_577_836_800
RESET_EPOCH_MAX = 4_102_444_800

# How many rollout files to look back through. Enough to cover a handful of
# terminals open at once; small enough that this stays a few stat calls and a
# tail read rather than a walk of a year of history.
RECENT_FILES = 6

# Only the end of a rollout file can hold the newest reading, and a long
# session's file is megabytes of conversation. Read the tail and parse what
# survives; a line cut in half at the front simply fails to parse and is
# skipped, which is the behaviour we would want from a malformed line anyway.
TAIL_BYTES = 256 * 1024


def sessions_root() -> str:
    """Where Codex keeps its rollout logs.

    Returns the path whether or not it exists: not having Codex installed is
    an ordinary state, handled by the reader rather than by pretending we do
    not know where to look. CODEX_HOME is honoured because Codex itself does.
    """
    home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    return os.path.join(home, "sessions")


def recent_rollouts(root: str = None, limit: int = RECENT_FILES):
    """The most recently modified rollout files, newest first."""
    root = sessions_root() if root is None else root
    try:
        paths = glob.glob(os.path.join(root, "*", "*", "*", "rollout-*.jsonl"))
    except OSError:
        return []
    out = []
    for p in paths:
        try:
            out.append((os.path.getmtime(p), p))
        except OSError:
            continue        # deleted between the glob and the stat
    out.sort(reverse=True)
    return [p for _, p in out[:limit]]


def _tail_lines(path: str):
    """The last TAIL_BYTES of a file, as lines. [] on any read failure."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > TAIL_BYTES:
                f.seek(size - TAIL_BYTES)
            blob = f.read()
    except OSError:
        return []
    try:
        text = blob.decode("utf-8", "replace")
    except Exception:
        return []
    return text.splitlines()


def _epoch(value):
    """A reset timestamp, or None when it is not one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not (RESET_EPOCH_MIN <= value <= RESET_EPOCH_MAX):
        return None
    return int(value)


def _pct(window):
    """`used_percent` out of one window object, or UNKNOWN.

    Range-checked rather than merely type-checked: a number outside 0-100
    means the field has changed meaning upstream, and the honest answer to
    that is "we don't know", not a saturated dial.
    """
    if not isinstance(window, dict):
        return base.UNKNOWN
    v = window.get("used_percent")
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return base.UNKNOWN
    if not (0 <= v <= 100):
        return base.UNKNOWN
    return float(v)


def _classify(rate_limits: dict):
    """(session_window, weekly_window) out of a `rate_limits` object.

    By declared length first, by position second. Both may be None.
    """
    if not isinstance(rate_limits, dict):
        return None, None
    primary = rate_limits.get("primary")
    secondary = rate_limits.get("secondary")

    session = weekly = None
    for w in (primary, secondary):
        if not isinstance(w, dict):
            continue
        minutes = w.get("window_minutes")
        if isinstance(minutes, bool) or not isinstance(minutes, (int, float)):
            continue
        if minutes <= SESSION_WINDOW_MAX_MIN:
            session = session or w
        else:
            weekly = weekly or w

    # Nothing declared its length. Fall back to the positional meaning, which
    # is right for every file seen so far and is only reached when the field
    # this prefers has gone away. Objects only: a string here reached
    # `.get("resets_at")` in the caller and took the source down.
    if session is None and weekly is None:
        session = primary if isinstance(primary, dict) else None
        weekly = secondary if isinstance(secondary, dict) else None
    return session, weekly


def _observed_at(line: dict, mtime: float) -> float:
    """When Codex wrote the reading, not when we read it.

    The event carries its own ISO-8601 timestamp, which survives the file
    being copied or touched. mtime is the fallback, and is what every other
    source here uses.
    """
    stamp = line.get("timestamp")
    if isinstance(stamp, str):
        try:
            # Python 3.11 parses "Z"; earlier versions do not, and the daemon
            # ships frozen against 3.12, so this is belt and braces.
            t = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            epoch = t.timestamp()
        except (ValueError, OverflowError, OSError):
            # OSError: Windows refuses to convert dates before 1970.
            return mtime
        # Bounded like resets_at below. A stamp from a wrong clock -- or a
        # crafted one -- in the far future would otherwise read as fresh
        # forever and win every recency contest.
        if RESET_EPOCH_MIN <= epoch <= RESET_EPOCH_MAX:
            return epoch
    return mtime


def parse_rollout_tail(lines, mtime: float):
    """The newest (rate_limits, observed_at) in these lines, or (None, None).

    Scanned backwards: the answer is at the end of the file, and a long
    session has thousands of lines that are not it.
    """
    for raw in reversed(lines):
        if "rate_limits" not in raw:
            continue        # cheap reject before the parse
        try:
            line = json.loads(raw)
        except ValueError:
            continue        # a half line at the head of the tail, or garbage
        if not isinstance(line, dict):
            continue
        payload = line.get("payload")
        if not isinstance(payload, dict):
            continue
        limits = payload.get("rate_limits")
        if isinstance(limits, dict):
            return limits, _observed_at(line, mtime)
    return None, None


# --- execution state ---------------------------------------------------------
#
# Codex has no hook interface, but its rollout log is a journal of the same
# transitions: `task_started` when a turn begins, `task_complete` when the
# answer is in, `turn_aborted` when the person interrupted it. The newest of
# these in a file is that session's state, aged by its own timestamp, with
# the same thresholds Claude's hooks use -- a turn silent past STUCK_AFTER_S
# is stuck, anything past ABANDONED_AFTER_S is a session that is gone.
#
# Permission prompts are not in the journal as far as anyone has observed, so
# there is no `waiting` for Codex; a prompt shows as `running` until it is
# answered, and as `stuck` if it is left long enough. Honest, if less useful.
from pc.providers.claude_state import (STUCK_AFTER_S, ABANDONED_AFTER_S,  # noqa: E402
                                       T_EPOCH_MIN, T_EPOCH_MAX)

STATE_SRC_ID = "cli-state"
_TURN_EVENTS = {
    "task_started": base.STATE_RUNNING,
    "task_complete": base.STATE_IDLE,
    "turn_aborted": base.STATE_IDLE,
}


def parse_rollout_state(lines, now_epoch, stuck_after_s=STUCK_AFTER_S):
    """The execution state one rollout file implies, or STATE_UNKNOWN.

    Scanned backwards like the rate limits: the newest turn event is the
    answer. A file with no turn event yet -- a session opened and not typed
    into -- makes no claim, for the same reason a Claude SessionStart does not.
    """
    for raw in reversed(lines):
        if "task_" not in raw and "turn_aborted" not in raw:
            continue        # cheap reject before the parse
        try:
            line = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(line, dict) or line.get("type") != "event_msg":
            continue
        payload = line.get("payload")
        if not isinstance(payload, dict):
            continue
        state = _TURN_EVENTS.get(payload.get("type"))
        if state is None:
            continue
        t = _observed_at(line, float("nan"))
        if not (T_EPOCH_MIN <= t <= T_EPOCH_MAX):
            return base.STATE_UNKNOWN   # no usable timestamp: no age, no claim
        age = now_epoch - t
        if age < 0:
            age = 0.0                   # a clock that stepped; treat as fresh
        if age > ABANDONED_AFTER_S:
            return base.STATE_UNKNOWN
        if state == base.STATE_RUNNING and age > stuck_after_s:
            return base.STATE_STUCK
        return state
    return base.STATE_UNKNOWN


class CodexCliProvider(base.ProviderParser):
    def __init__(self, root=None, stuck_after_s=STUCK_AFTER_S):
        self._root = root
        self._stuck_after = stuck_after_s

    def get_provider_id(self) -> str:
        return PROVIDER_ID

    def root(self):
        return self._root if self._root is not None else sessions_root()

    def parse_cli_event(self, raw_payload, now_epoch, observed_at):
        """One `rate_limits` object, already read, as a frame.

        Returns None when neither window yields a percentage: a frame with no
        numbers must not be allowed to win a recency contest for numbers it
        does not have.
        """
        session, weekly = _classify(raw_payload)
        s_pct, w_pct = _pct(session), _pct(weekly)
        if s_pct < 0 and w_pct < 0:
            return None
        return base.NormalizedUsageFrame(
            provider=PROVIDER_ID,
            src=SRC_ID,
            observed_at=observed_at,
            session_pct=s_pct,
            session_resets_at=_epoch((session or {}).get("resets_at")),
            weekly_pct=w_pct,
            weekly_resets_at=_epoch((weekly or {}).get("resets_at")),
            stale=(now_epoch - observed_at) > STALE_AFTER_S,
        )

    def poll(self, now_epoch):
        """The freshest reading Codex has written, as a one-frame list.

        One frame, not one per rollout file: the two percentages are
        account-wide, so several open terminals all describe the same pair of
        windows and handing the normalizer six copies of it would only make
        the freshest one win a contest it has already won here.
        """
        best = None
        counts = {}
        for path in recent_rollouts(self._root):
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            lines = _tail_lines(path)
            # Every rollout is one session, so every one of them votes on
            # the execution state -- unlike the percentages, which are one
            # account-wide pair however many terminals are open.
            state = parse_rollout_state(lines, now_epoch, self._stuck_after)
            if state != base.STATE_UNKNOWN:
                counts[state] = counts.get(state, 0) + 1
            limits, observed_at = parse_rollout_tail(lines, mtime)
            if limits is None:
                continue
            frame = self.parse_cli_event(limits, now_epoch, observed_at)
            if frame is None:
                continue
            if best is None or frame.observed_at > best.observed_at:
                best = frame
        frames = [best] if best is not None else []
        if counts:
            # A separate frame with no percentages, exactly as Claude's state
            # provider does it: it can never win a recency contest for
            # numbers, and the normalizer merges its state field by field.
            frames.append(base.NormalizedUsageFrame(
                provider=PROVIDER_ID,
                src=STATE_SRC_ID,
                observed_at=now_epoch,
                state=base.worst_of(counts),
                n_run=counts.get(base.STATE_RUNNING, 0),
                n_idle=counts.get(base.STATE_IDLE, 0),
                n_stuck=counts.get(base.STATE_STUCK, 0),
            ))
        return frames
