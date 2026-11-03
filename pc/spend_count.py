"""The running count of spend between /usage runs.

/usage saves an exact anchor; between runs the only live signal is the
status line's per-session `cost.total_cost_usd`, a cumulative figure that
each Claude Code process renders on its own. This turns those samples into
counted cents.

Rules, each from a way the raw number misleads:

* A session's first sighting adds nothing. A resumed session shows its whole
  past cost on the first render, and that is not new spend.
* Only a rise over the session's high-water mark counts. Two processes on one
  session id show different totals for the same session; a lower figure from
  the lagging one must not be counted again when the other catches up.
* A rise is stamped with the previous sighting of its session. `since(anchor)`
  counts only rises whose previous sighting is at or after the anchor: a rise
  between a sighting before the anchor and one after it may have happened
  partly before /usage measured, so it is dropped rather than counted twice.
* `at` is the render's own time (the payload's mtime), so reading the same
  render again moves nothing.

Integer cents throughout. State is per session UUID (a per-session total is
all the spec allows) and is never printed or logged.
"""
import json
import math
import os
import tempfile

import protocol

_BUCKETS = 20
_KEEP_S = 32 * 86400
_VERSION = 1


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) \
        and math.isfinite(x)


def _default_path() -> str:
    # Resolved when called: a module constant would read the real home at
    # import, before the test sandbox runs.
    return os.path.join(os.path.expanduser("~"), ".blink", "spend-count.json")


class SpendCounter:
    def __init__(self, path=None, persist=True):
        self._path = path
        self._persist = persist
        self._fp = None
        self._period = None
        self._sessions = {}   # sid -> [high_water_cents, last_seen]
        self._adds = []       # [prev_seen, at, cents]
        self._dirty = False
        if persist:
            self._load()

    # -- state -----------------------------------------------------------
    def _file(self) -> str:
        return self._path or _default_path()

    def _clear(self):
        self._sessions = {}
        self._adds = []
        self._dirty = True

    def _load(self):
        try:
            with open(self._file(), "r", encoding="utf-8") as f:
                d = json.load(f)
            if not isinstance(d, dict) or d.get("v") != _VERSION:
                return
            fp, period = d.get("fp"), d.get("period")
            if fp is not None and not isinstance(fp, str):
                return
            if period is not None and not isinstance(period, (int, float)):
                return
            if not isinstance(d["sessions"], dict) \
                    or not isinstance(d["adds"], list):
                return
            sessions = {}
            for sid, v in d["sessions"].items():
                hw, seen = v
                if not isinstance(sid, str) or isinstance(hw, bool) \
                        or not isinstance(hw, int) or not _num(seen):
                    return
                sessions[sid] = [hw, seen]
            adds = []
            for prev, at, cents in d["adds"]:
                # json accepts Infinity and NaN; int(inf) would raise.
                if not all(_num(x) for x in (prev, at, cents)):
                    return
                adds.append([prev, at, int(cents)])
        except (OSError, ValueError, KeyError, TypeError):
            return
        self._fp, self._period = fp, period
        self._sessions, self._adds = sessions, adds

    def bind(self, account_fp, period_start) -> None:
        if self._fp is None and self._period is None:
            # Nothing stored or loaded: adopt without discarding what was
            # observed before the first look.
            self._fp, self._period = account_fp, period_start
            self._dirty = True
            return
        if account_fp != self._fp or period_start != self._period:
            self._fp, self._period = account_fp, period_start
            self._clear()

    # -- counting --------------------------------------------------------
    def observe(self, session_id, cost_usd, at) -> bool:
        # The counter owns the guard: a NaN, infinity, string or bool from
        # a damaged payload must not touch the state.
        if not _num(cost_usd) or not _num(at):
            return False
        cents = int(round(cost_usd * 100))
        s = self._sessions.get(session_id)
        if s is None:
            self._sessions[session_id] = [cents, at]
            self._dirty = True
            return True
        hw, last = s
        changed = False
        if cents > hw:
            self._adds.append([last, at, cents - hw])
            s[0] = cents
            changed = True
        if at > last:
            s[1] = at
            changed = True
        if changed:
            cutoff = at - _KEEP_S
            self._adds = [a for a in self._adds if a[1] >= cutoff]
            self._dirty = True
        return changed

    def since(self, anchor_at) -> int:
        return sum(c for prev, _at, c in self._adds if prev >= anchor_at)

    def buckets(self, now) -> list:
        out = [0] * _BUCKETS
        q = protocol.burn_quarter(now)
        for _prev, at, c in self._adds:
            i = _BUCKETS - 1 - (q - protocol.burn_quarter(at)) // 900
            if 0 <= i < _BUCKETS:
                out[int(i)] += c
        return out

    # -- persistence -----------------------------------------------------
    def save(self) -> None:
        if not self._persist or not self._dirty:
            return
        doc = {"v": _VERSION, "fp": self._fp, "period": self._period,
               "sessions": self._sessions, "adds": self._adds}
        target = self._file()
        tmp = None
        try:
            d = os.path.dirname(target) or "."
            os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".spend-count.", dir=d)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(doc, f, separators=(",", ":"))
            os.replace(tmp, target)
            tmp = None
            self._dirty = False
        except OSError:
            # Keep the count in memory and try again at the next save.
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
