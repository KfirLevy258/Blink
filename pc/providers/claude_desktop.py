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
import sys

from pc import desktop_paths
from pc.providers import base

PROVIDER_ID = "claude"
SRC_ID = "desktop"

# Same bound, and for the same reason, as pc/statusline_source.STALE_AFTER_S:
# the app rewrites this roughly every five minutes WHILE IT RUNS, so age here
# measures how long ago the app was last open, not how wrong the numbers are.
# Half an hour keeps ordinary pauses silent without presenting an abandoned
# app as live.
STALE_AFTER_S = 1800

# A session reading older than a window is not offered: base.session_outlived.
#
# A five-hour window that was open when the sample was taken has certainly
# ended five hours later, and this source has no reset stamps to say so
# itself, so without a bound the figure stayed on the bus for as long as the
# file did: on 2026-10-30 a sample from 37 hours before a verified Ultra
# account stopped having a window was still filling the session dial, and it
# is the same shape on a restarted daemon, which has no memory of the
# account. Past the bound the session is UNKNOWN (and rateless: the rate is
# refused past BURN_MAX_GAP_S anyway), and the weekly figure, which lasts
# seven days, is kept. `stale` is a separate and earlier judgement (half an
# hour): between the two the number is shown, honestly aged. The bound and
# its slack are base's, where the normalizer's window length lives too; this
# module spelled them out on its own until review (2026-10-31).

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
    by pretending we do not know where to look. The folder it sits in is
    pc/desktop_paths's to find, because the Store build moves it.
    """
    return desktop_paths.usage_cache_path()


# What `blink status` can say about the cache file, from ClaudeDesktopProvider
# .verdict(). poll() collapses all but the first into "no frame", which is
# right for the daemon and was wrong for status: it printed "did not parse --
# please report it" for a cache the app simply had not filled yet, and people
# reported a fault that was not there (found by destaben in their fork,
# 2026-10-19). Each of these is a different thing for the reader to do.
VERDICT_FRAME = "frame"            # a reading: the normal case
VERDICT_UNFILLED = "unfilled"      # the app's own empty shape: wait for it
VERDICT_NOT_OURS = "not-ours"      # JSON, but no layout we know: report it
VERDICT_GARBLED = "garbled"        # not JSON, or not UTF-8: report it
VERDICT_UNREADABLE = "unreadable"  # could not be opened or read at all


def read_text(path):
    """The cache file's text, or None when its bytes are not UTF-8.

    The one reader for this file: the provider and the weekly anchor's
    DesktopHistoryProvider both come through here. Read as bytes and decoded
    here, not with open(encoding="utf-8"): that raises UnicodeDecodeError, a
    ValueError rather than an OSError, and it went straight out of poll() --
    a torn write or a UTF-16 file took `blink status` down with a traceback,
    losing every row after Desktop (review, 2026-10-25). OSError still
    propagates; the callers each have their own answer to it.
    """
    with open(path, "rb") as f:
        raw = f.read()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


# What parse_json returns for content that is not a JSON document. A
# sentinel, not None: `null` is valid JSON, and it is a format change to
# report rather than a garbled file.
NOT_JSON = object()


def parse_json(content):
    """The document in `content`, or NOT_JSON. Never raises.

    The one place this file's text becomes a document, for the provider and
    the weekly anchor alike. RecursionError is caught with ValueError: deep
    enough nesting (how deep depends on the interpreter) makes json.loads
    raise it, it is neither a
    ValueError nor an OSError, and it went out of every except clause that
    read this file, taking `blink status` down at the Desktop row (review,
    2026-10-25). TypeError is None from read_text, bytes that were not UTF-8.
    """
    try:
        return json.loads(content)
    except (TypeError, ValueError, RecursionError):
        return NOT_JSON


def _version(doc):
    """The file's own `version`, when it is a scalar we could dispatch on,
    else None. The one place that decides it, for parse_cache_file,
    raw_samples and _is_unfilled_doc alike.

    bool is refused first: True == 1 in Python, so `"version": true` would
    otherwise be looked up as version 1. And only a scalar: a list or object
    here once raised TypeError from the dict lookup -- outside every try in
    parse_cache_file -- and the bus marked the whole source broken over one
    odd field.
    """
    version = doc.get("version") if isinstance(doc, dict) else None
    if isinstance(version, bool) or not isinstance(version, (int, str)):
        return None
    return version


def _is_unfilled_doc(doc) -> bool:
    """The two shapes of a cache the app has created but not yet sampled
    into: an empty object, or a version we have a parser for with an empty
    `samples` list and no other key. Nothing else qualifies. Keyed off
    _PARSERS rather than a literal 2, so the version that adds a parser is
    not reported as a format change while it is still empty.

    Exact on purpose. The first version of this check asked only "is it
    valid JSON", and the app always writes JSON, so renamed sample keys, a
    `t` that moved to seconds, a version 3 with a new array and a bare
    `null` all came out as "not filled yet" (review, 2026-10-25). A false
    report costs one issue; a false all-clear hides a broken source.
    """
    if doc == {}:
        return True
    if not isinstance(doc, dict) or set(doc) != {"version", "samples"}:
        return False
    if _version(doc) not in _PARSERS:
        return False
    return isinstance(doc["samples"], list) and not doc["samples"]


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
    # Upper bound is 1000, not 100: extra usage carries a window past its
    # limit and 102 is a real reading, not a changed field. Clamping it to
    # UNKNOWN here drew an empty ring at the moment the user went over.
    return v if 0 <= v <= 1000 else base.UNKNOWN


def _sample_to_frame(sample: dict, now_epoch=None):
    """(frame-shaped dict, observed_at) from one sample, or (None, None).

    With `now_epoch`, a session figure older than a window is retired
    (base.session_outlived): it comes back UNKNOWN. A sample whose only
    figure has retired is still returned, with both figures unknown, and
    _newest_sample ranks it below any sample that says more. The rate's
    series reads samples without a clock, since it judges age its own way.
    """
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
    if now_epoch is not None and base.session_outlived(observed_at, now_epoch):
        fh = base.UNKNOWN
    return {"session_pct": fh, "weekly_pct": sd}, observed_at


def _newest_sample(samples, now_epoch=None):
    """The most recent usable sample.

    Chosen by MAXIMUM timestamp rather than by taking samples[-1]. The real
    file is sorted ascending and samples[-1] is correct today -- but that is
    an observation about one file, not a guarantee from an application we do
    not control, and the cost of being wrong is silently showing an old
    reading as current. max() costs one pass over a list that is a few
    thousand entries at most.

    A sample with a figure still standing outranks every sample whose only
    figure has retired (_sample_to_frame), however much newer: retired, a
    newest sample with fh and no sd says nothing, and the weekly figure in
    the samples before it must still be offered (review, 2026-10-31). When
    every sample is retired, the newest of them is the answer, with both
    figures unknown: the file is still the app's and still read -- `blink
    status` must not call it a changed format -- so it stands with nothing
    to offer but its age. Alone on the bus that sends nothing, after five
    hours and ten minutes with no sample, which means Claude Desktop has
    been closed that long: the board went to sleep at four hours without
    contact (firmware SLEEP_ABSENT_AFTER_S), so the reading it last drew is
    not on screen. That answer used to come from a second pass over the
    file without the clock, which could only ever find the same.
    """
    best, best_rank = None, None
    for s in samples:
        fields, observed_at = _sample_to_frame(s, now_epoch)
        if fields is None:
            continue
        rank = (fields["session_pct"] >= 0 or fields["weekly_pct"] >= 0,
                observed_at)
        if best_rank is None or rank > best_rank:
            best, best_rank = fields, rank
    return best, (best_rank[1] if best_rank is not None else None)


# --- burn rate -----------------------------------------------------------
#
# How fast the five-hour window is filling, for the one configuration that has
# percentages and no reset time: Claude Desktop with no Claude Code.
#
# This is NOT a reset time in disguise. Deriving the reset from this file was
# investigated and rejected on 2026-08-28, with numbers taken from a real
# month of samples on the author's machine:
#
#   - This file carries no reset timestamp, and for a long time nothing on
#     disk did -- the survey behind this comment searched every JSON file,
#     the LevelDB stores, Session Storage, IndexedDB and the preferences
#     plist on 2026-08-28 and found none. That changed: Claude Desktop's
#     Local Storage now carries a five-hour `resetsAt`, which
#     pc/providers/claude_desktop_ls reads. The seven-day reset is still
#     absent for a chat-only user. See
#     docs/research/claude-desktop-window-sources.md.
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
# 3600, not 1800. BURN_MAX_GAP_S was raised to 1000 s to admit Claude
# Desktop's 15-minute idle cadence, but a 1800 s window sampled every 900 s
# holds exactly BURN_MIN_SAMPLES points -- so one dropped sample and the rate
# vanishes, which is the symptom raising the gap was meant to cure. An hour
# holds four or five at that cadence and still only spans readings the gap
# guard has already accepted as consecutive.
BURN_WINDOW_S = 3600.0

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
# (re-read in 1.40609.0 on 2026-08-31: the cadence had already changed once,
# see BURN_WINDOW_S below -- which is why no interval here is load-bearing)
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


def _samples_array_by_shape(doc):
    """The raw samples array by shape alone, for a version we have never
    seen -- the search _parse_by_shape performs below, pulled out so a
    second consumer (raw_samples, for a caller that wants the series rather
    than the newest reading) can reuse it instead of growing its own copy.

    Deliberately shape-driven rather than a guess at a specific past or future
    layout. Writing a `_parse_v1` for a file nobody here has ever observed
    would be inventing a schema and then testing against the invention; this
    instead asks the only question that matters -- is there a list of samples
    anywhere obvious -- and gives up cleanly when the answer is no.
    """
    if isinstance(doc, list):
        return doc if doc else None
    if isinstance(doc, dict):
        for key in ("samples", "history", "usage"):
            v = doc.get(key)
            if isinstance(v, list) and v:
                return v
    return None


def _parse_v2(doc, now_epoch=None):
    """The layout observed in the wild: {"version": 2, "samples": [...]}."""
    samples = doc.get("samples")
    if not isinstance(samples, list) or not samples:
        return None, None
    return _newest_sample(samples, now_epoch)


def _parse_by_shape(doc, now_epoch=None):
    """Last resort for a version we have never seen."""
    samples = _samples_array_by_shape(doc)
    if samples is None:
        return None, None
    return _newest_sample(samples, now_epoch)


# Versioned adapters, dispatched on the file's own `version` field. The file
# self-versions, which is the whole reason this can be a table rather than a
# pile of hasattr checks.
_PARSERS = {2: _parse_v2}


def raw_samples(doc):
    """The file's own samples array, undoctored, or None.

    Dispatched the same way parse_cache_file dispatches a single reading --
    by the file's own `version` field, falling back to shape -- but this
    returns the whole series rather than reducing it to the newest point.

    Built for pc.providers.weekly_anchor's refutation check, which needs a
    sequence of readings to notice a drop, not the latest percentage. There
    is exactly one parser for plan-usage-history.json in this codebase; this
    is that parser's other exit, not a second one.
    """
    # Keyed off _PARSERS, as parse_cache_file and _is_unfilled_doc are, so a
    # new version's parser is not dispatched differently here. Every parser
    # so far keeps its series under `samples`; one that does not must say
    # so here too.
    if _version(doc) in _PARSERS:
        samples = doc.get("samples")
        return samples if isinstance(samples, list) and samples else None
    return _samples_array_by_shape(doc)


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
        doc = parse_json(file_content)
        if doc is NOT_JSON:
            return None
        return self._frame_from_doc(doc, now_epoch, observed_at)

    def _frame_from_doc(self, doc, now_epoch, observed_at=None):
        """parse_cache_file's work on a document already loaded, so the
        verdict can decide from one json.loads rather than two."""
        # _version refuses non-scalars and bools; see there for the incident.
        parser = _PARSERS.get(_version(doc), _parse_by_shape)
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
            # Every sample's only figure a retired session still comes back,
            # both figures unknown, at the newest sample's time: see
            # _newest_sample.
            fields, sample_at = parser(doc, now_epoch)
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
        # A retired session figure (_sample_to_frame) has no rate to retire
        # with it: session_burn_pph refuses any series whose newest sample
        # is over BURN_MAX_GAP_S old, so by that age `burn` is already None
        # (pinned in test_no_session_window).
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

    def _classify(self, content, now_epoch):
        """(VERDICT_*, frame or None) for text read_text returned, with no
        side effect at all. The one decision behind both verdict() and
        poll(), so the daemon's log and `blink status` cannot disagree about
        the same bytes."""
        doc = parse_json(content)
        if doc is NOT_JSON:
            return VERDICT_GARBLED, None
        frame = self._frame_from_doc(doc, now_epoch)
        if frame is not None:
            return VERDICT_FRAME, frame
        if _is_unfilled_doc(doc):
            return VERDICT_UNFILLED, None
        return VERDICT_NOT_OURS, None

    def verdict(self, now_epoch):
        """(one of the VERDICT_* constants, frame or None), from ONE read.

        For `blink status`. One read rather than poll() followed by a second
        look: the app rewrites the file every few minutes, so two reads can
        see two different files and describe neither. Side-effect free --
        never prints, which poll()'s once-per-path complaint did, right above
        the status row it contradicted -- and never raises.
        """
        try:
            content = read_text(self.path())
        except OSError:
            return VERDICT_UNREADABLE, None
        except Exception:
            return VERDICT_GARBLED, None
        return self._classify(content, now_epoch)

    def poll(self, now_epoch):
        path = self.path()
        try:
            content = read_text(path)
        except OSError:
            # Not installed, not readable, not running. All normal, all
            # silent: this is an ambient source and its absence is not news.
            return []
        verdict, frame = self._classify(content, now_epoch)
        if verdict == VERDICT_FRAME:
            self._complained_about = None
            return [frame]
        if verdict == VERDICT_UNFILLED:
            # The app before its first sample: nothing to read and nothing
            # wrong. Complaining here put "did not parse" in bridge.log on
            # every start of a freshly signed-in desk, and under the status
            # row that called the same file not filled yet (review,
            # 2026-10-25). Reset, so a later real format change is still
            # said once.
            self._complained_about = None
            return []
        if self._complained_about != path:
            print(f"[desktop] {path} did not parse as a usage cache;"
                  " ignoring this source", file=sys.stderr)
            self._complained_about = path
        return []
