"""A number of seconds from the environment, or the default when it is not one.

Shared by every test-only cadence override (BLINK_POLL_INTERVAL_S for the
usage heartbeat, BLINK_STATUS_POLL_S for the status pages), because each one
used to carry its own copy of the checks and the copies drifted: the status
page's first version let "inf" through, which stopped its polling for good,
and quietly turned "nan", "0" and "-5" into one second.
"""
import math
import sys


def seconds_from_env(env, name, default, prefix="[bridge]"):
    """`env[name]` as a positive, finite float, else `default`.

    Malformed or non-positive is refused rather than obeyed: 0 or a negative
    makes a `now >= next` gate true on every turn of the loop that checks it,
    which polls as fast as the loop spins. It falls back to the default and
    says so, once per call -- callers resolve this at startup.

    Non-finite is refused for the opposite reason, and is the quieter hazard
    of the two. "nan", "inf" and "1e400" all parse without raising, and a
    positivity check alone lets them through -- every comparison against nan
    is False, so `nan <= 0` is False. Either one poisons
    `next = now + interval`, after which `now >= next` is False for the rest
    of the run: polling stops dead and nothing anywhere says why. Hence
    isfinite FIRST, before any comparison that nan would win by default.
    """
    raw = env.get(name)
    if raw is None:
        return default
    try:
        seconds = float(raw)
    except ValueError:
        seconds = 0
    if not math.isfinite(seconds) or seconds <= 0:
        # "usable" rather than "positive": inf IS positive, and telling
        # someone who typed it that it is not would send them looking in the
        # wrong direction.
        print(f"{prefix} {name}={raw!r} is not a usable number of seconds;"
              f" polling every {default} s.", file=sys.stderr)
        return default
    return seconds
