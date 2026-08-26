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
        parser = _PARSERS.get(version, _parse_by_shape)
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
