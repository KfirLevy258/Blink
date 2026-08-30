"""Claude Desktop's own usage cache, read as an ambient background source.

This is an internal file belonging to an application we do not control, so it
is treated as unstable by design: every field is checked, every failure is
local, and an unrecognised layout downgrades this source to silence rather
than taking the daemon down with it. The CLI hook remains authoritative when
both are available -- see pc/normalizer.py for why.

Two things about the real file are worth stating here, because both are
absent from the specification this was built from and both are the kind of
mistake that produces a confidently wrong panel rather than an error:

  - `samples[N].t` is in MILLISECONDS. Read as seconds it lands in the year
    56649, every freshness check passes trivially, and a reading from any
    point in history presents as current. Verified against a real 2215-sample
    file, 2026-08-26.

  - The cache carries NO reset timestamps. It has percentages and nothing to
    say about when either window rolls over. A frame from here therefore has
    session_resets_at=None, which is not a parse failure -- it is this
    source's honest shape, and it is the reason the normalizer merges field
    by field instead of picking one winning source per poll.
"""
import json
import os
import sys

from pc.providers import base

PROVIDER_ID = "claude"
SRC_ID = "desktop"

# Same bound, and for the same reason, as pc/statusline_source.STALE_AFTER_S:
# the app rewrites this roughly every five minutes WHILE IT RUNS, so age here
# measures how long ago the app was last open, not how wrong the numbers are.
# Half an hour keeps ordinary pauses silent without presenting an abandoned
# app as live.
STALE_AFTER_S = 1800

# Milliseconds, per the module docstring. Named rather than inlined so the
# unit is stated at every use.
MS_PER_S = 1000.0

# Plausible bounds for a sample timestamp: 2020-01-01 to 2100-01-01.
#
# Without these one bad sample is permanent. `_pick` in the normalizer ranks
# strictly by observed_at, and `stale` is `now - observed_at > 1800` -- so a
# timestamp in the far future is never stale AND beats every real reading,
# forever, pinning the panel to whatever that sample said. It costs nothing to
# refuse: codex_cli has had exactly this guard on `resets_at` since it was
# written, for exactly this reason.
SAMPLE_EPOCH_MIN = 1_577_836_800
SAMPLE_EPOCH_MAX = 4_102_444_800


def cache_path():
    """Where the desktop app keeps its usage history on this platform.

    Returns the path whether or not it exists -- absence is a normal state
    (the app may simply not be installed) and is handled by the reader, not
    by pretending we do not know where to look.
    """
    if sys.platform == "darwin":
        return os.path.expanduser(
            "~/Library/Application Support/Claude/plan-usage-history.json")
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            # Fall back rather than crash: a Windows session with no APPDATA
            # is broken in ways that are not ours to fix, but it must not
            # take the daemon with it.
            appdata = os.path.expanduser("~\\AppData\\Roaming")
        return os.path.join(appdata, "Claude", "plan-usage-history.json")
    return os.path.expanduser("~/.config/Claude/plan-usage-history.json")


def _pct(u: dict, key: str) -> float:
    """One percentage out of a sample's `u` object, or UNKNOWN.

    Range-checked, not merely type-checked. A number outside 0-100 means the
    field was misread or has changed meaning upstream, and the correct
    response to that is "--" rather than a meter pinned to something absurd.
    """
    try:
        v = float(u[key])
    except (KeyError, TypeError, ValueError):
        return base.UNKNOWN
    return v if 0 <= v <= 100 else base.UNKNOWN


def _sample_to_frame(sample: dict):
    """(frame-shaped dict, observed_at) from one sample, or (None, None)."""
    if not isinstance(sample, dict):
        return None, None
    u = sample.get("u")
    if not isinstance(u, dict):
        return None, None
    try:
        observed_at = float(sample["t"]) / MS_PER_S
    except (KeyError, TypeError, ValueError):
        return None, None
    if not (SAMPLE_EPOCH_MIN <= observed_at <= SAMPLE_EPOCH_MAX):
        # Out of range means the field was misread or changed units upstream
        # (it is milliseconds today; microseconds would land in the year
        # 56649). Dropping the sample is right either way -- see the constants.
        return None, None
    fh = _pct(u, "fh")
    sd = _pct(u, "sd")
    if fh < 0 and sd < 0:
        # A sample carrying neither percentage tells us nothing. Returning it
        # anyway would let it win a recency contest against a real reading
        # purely by being newer, which is how a fresh empty source blanks a
        # good stale one.
        return None, None
    return {"session_pct": fh, "weekly_pct": sd}, observed_at


def _newest_sample(samples):
    """The most recent usable sample.

    Chosen by MAXIMUM timestamp rather than by taking samples[-1]. The real
    file is sorted ascending and samples[-1] is correct today -- but that is
    an observation about one file, not a guarantee from an application we do
    not control, and the cost of being wrong is silently showing an old
    reading as current. max() costs one pass over a list that is a few
    thousand entries at most.
    """
    best, best_t = None, None
    for s in samples:
        fields, observed_at = _sample_to_frame(s)
        if fields is None:
            continue
        if best_t is None or observed_at > best_t:
            best, best_t = fields, observed_at
    return best, best_t


# --- burn rate -----------------------------------------------------------
#
# How fast the five-hour window is filling, for the one configuration that has
# percentages and no reset time: Claude Desktop with no Claude Code.
#
# This is NOT a reset time in disguise. Deriving the reset from this file was
# investigated and rejected on 2026-08-28, with numbers taken from a real
# month of samples on the author's machine:
#
#   - No reset timestamp is persisted anywhere on disk. Every JSON file, the
#     LevelDB stores, Session Storage, IndexedDB and the preferences plist
#     were searched. The only copies that exist are in Claude Code's status
#     line payload and in evicting HTTP cache entries.
#   - The five-hour window is ROLLING -- observed gaps between resets ran
#     4.95, 5.5, 5.9, 16.0, 17.8 and 32.2 hours -- so a past reset does not
#     predict the next one.
#   - It could be computed from the sample series (a window measures 5.00 h
#     from the first non-zero sample after a reset, and the server quantises
#     reset times to a 10-minute grid). But the app records only while it is
#     open: 18% of the month had samples at all, the longest gap was 409
#     hours, and only 13% of windows had an observable start. A method that
#     works one time in eight, whose failure looks exactly like its success,
#     is not a method.
#
# A rate needs none of that. It needs only recent samples, which exist exactly
# when the app is open -- which is exactly when someone is looking at the
# panel. Everything below is arithmetic on readings we actually saw.

# How far back to measure. Long enough to average out the 5-minute sampling
# granularity, short enough that the answer describes now rather than the
# last hour of a session that has since changed pace.
BURN_WINDOW_S = 1800.0

# A gap beyond this means the app was CLOSED, and a rate averaged across time
# we did not observe is the same mistake as deriving the reset.
#
# This was 600 s, chosen when the cadence was a flat 300 s (median; p90 330 s,
# measured over 1671 intervals). That is no longer the whole cadence. Claude
# Desktop fetches on TWO schedules -- every 300 s while the machine is in use,
# every 900 s otherwise -- so 600 s sat BELOW the app's own idle interval and
# refused every ordinary reading taken while its owner was present but not
# typing. The rate did not go wrong, it went ABSENT, which on a Desktop-only
# panel is the one line under the gauge (there is no countdown to fall back
# to) and so showed a permanent "--".
#
# 1000 s clears the 900 s interval with enough margin for jitter and still
# refuses the case the guard exists for: an app closed and reopened leaves a
# gap of hours, not of sixteen minutes.
#
# Read out of Claude.app 1.37937.3's own bundle and then confirmed against it
# running, 2026-08-30. Both intervals belong to an application we do not
# control -- if the rate disappears again, measure the gaps in
# plan-usage-history.json before touching anything else.
BURN_MAX_GAP_S = 1000.0

# Below this the two endpoints are too close together for the slope to mean
# anything: a single 5-minute step would swing it by the whole window.
BURN_MIN_SPAN_S = 600.0

# Fewer points than this is a line drawn through noise.
BURN_MIN_SAMPLES = 3


def session_burn_pph(samples, now_epoch, window_s=BURN_WINDOW_S):
    """Percent per hour the session window is filling, or None.

    None is the common answer and the safe one. Every refusal below is a case
    where a number could be produced and would not describe reality.
    """
    if not isinstance(samples, list):
        return None

    pts = []
    for s in samples:
        fields, at = _sample_to_frame(s)
        if fields is None or fields["session_pct"] < 0:
            continue
        pts.append((at, fields["session_pct"]))
    if len(pts) < BURN_MIN_SAMPLES:
        return None
    pts.sort()

    # The newest reading has to be current. An app closed twenty minutes ago
    # leaves a perfectly computable rate that describes a session which has
    # already ended.
    if now_epoch - pts[-1][0] > BURN_MAX_GAP_S:
        return None

    tail = [p for p in pts if p[0] >= pts[-1][0] - window_s]
    if len(tail) < BURN_MIN_SAMPLES:
        return None

    span = tail[-1][0] - tail[0][0]
    if span < BURN_MIN_SPAN_S:
        return None

    for (t0, p0), (t1, p1) in zip(tail, tail[1:]):
        # A hole: the app was shut. Refuse rather than average across it.
        if t1 - t0 > BURN_MAX_GAP_S:
            return None
        # A reset inside the window. The percentage fell because the window
        # rolled, not because usage went backwards -- which it cannot do --
        # so a slope spanning it is meaningless in both directions.
        if p1 < p0:
            return None

    rate = (tail[-1][1] - tail[0][1]) / (span / 3600.0)
    # Not negative by construction, given the check above; the guard is for
    # the degenerate equal-endpoints case rather than for arithmetic.
    return rate if rate > 0 else None


def _parse_v2(doc):
    """The layout observed in the wild: {"version": 2, "samples": [...]}."""
    samples = doc.get("samples")
    if not isinstance(samples, list) or not samples:
        return None, None
    return _newest_sample(samples)


def _parse_by_shape(doc):
    """Last resort for a version we have never seen.

    Deliberately shape-driven rather than a guess at a specific past or future
    layout. Writing a `_parse_v1` for a file nobody here has ever observed
    would be inventing a schema and then testing against the invention; this
    instead asks the only question that matters -- is there a list of samples
    anywhere obvious -- and gives up cleanly when the answer is no.
    """
    if isinstance(doc, list):
        return _newest_sample(doc)
    if isinstance(doc, dict):
        for key in ("samples", "history", "usage"):
            v = doc.get(key)
            if isinstance(v, list) and v:
                return _newest_sample(v)
    return None, None


# Versioned adapters, dispatched on the file's own `version` field. The file
# self-versions, which is the whole reason this can be a table rather than a
# pile of hasattr checks.
_PARSERS = {2: _parse_v2}


class ClaudeDesktopProvider(base.ProviderParser):
    def __init__(self, path=None, stale_after_s=STALE_AFTER_S):
        self._path = path
        self._stale_after = stale_after_s
        # Remembered so an unparseable file is reported once rather than on
        # every poll for as long as the app stays broken.
        self._complained_about = None

    def get_provider_id(self) -> str:
        return PROVIDER_ID

    def path(self):
        return self._path if self._path is not None else cache_path()

    def parse_cache_file(self, file_content, now_epoch, observed_at=None):
        """A NormalizedUsageFrame from the cache's contents, or None.

        `observed_at` is ignored when the samples carry their own timestamp,
        which they do. A sample's own `t` is a better answer than the file's
        mtime: the app rewrites the whole file on every sample, so mtime says
        when it last wrote ANYTHING while `t` says when this reading was
        taken. They agree in the normal case and diverge in exactly the case
        that matters -- a file rewritten with no new sample in it.
        """
        try:
            doc = json.loads(file_content)
        except (TypeError, ValueError):
            return None

        version = doc.get("version") if isinstance(doc, dict) else None
        # Only a scalar can be looked up. A list or object here raised
        # TypeError from dict.get -- outside every try in this function, so
        # the bus marked the whole source broken over one odd field.
        if not isinstance(version, (int, str)) or isinstance(version, bool):
            version = None
        parser = _PARSERS.get(version, _parse_by_shape)
        # The rate needs the whole series, not the newest point. Read off the
        # same document the parser is about to reduce, and never let a failure
        # here cost the reading itself -- the percentages are the product and
        # the rate is a bonus for one configuration.
        raw = doc.get("samples") if isinstance(doc, dict) else doc
        try:
            burn = session_burn_pph(raw, now_epoch)
        except Exception:
            burn = None
        try:
            fields, sample_at = parser(doc)
        except Exception:
            # A parser is not allowed to take the daemon down over an
            # upstream layout change. This is the graceful-degradation path:
            # the source goes quiet, the CLI hook carries on.
            return None
        if fields is None:
            return None

        if sample_at is None:
            sample_at = observed_at if observed_at is not None else now_epoch

        stale = (now_epoch - sample_at) > self._stale_after
        return base.NormalizedUsageFrame(
            provider=PROVIDER_ID, src=SRC_ID, observed_at=sample_at,
            session_pct=fields["session_pct"],
            weekly_pct=fields["weekly_pct"],
            # No reset timestamps in this source at all -- see the module
            # docstring. None, not a guess.
            session_resets_at=None, weekly_resets_at=None,
            stale=stale,
            session_burn_pph=burn,
        )

    def poll(self, now_epoch):
        path = self.path()
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError:
            # Not installed, not readable, not running. All normal, all
            # silent: this is an ambient source and its absence is not news.
            return []
        frame = self.parse_cache_file(content, now_epoch)
        if frame is None:
            if self._complained_about != path:
                print(f"[desktop] {path} did not parse as a usage cache;"
                      " ignoring this source", file=sys.stderr)
                self._complained_about = path
            return []
        self._complained_about = None
        return [frame]
