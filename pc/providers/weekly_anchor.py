"""The remembered seven-day boundary, offered as a frame.

Contributes one field and dates it honestly. `observed_at` is when the
boundary was ORIGINALLY seen, not now, so pc/normalizer -- which ranks by
recency per field -- prefers any live source without needing to know this
provider exists.

Publishing is conditioned on `weekly_anchor.refuted_by` as well as on
`project`. `project` alone only ever withdraws on the eight-week
uncorroborated timeout; the spec's actual promise -- publish only while
nothing contradicts this anchor -- needs the percentage-drop check too, or a
refuted anchor keeps appearing on the panel for up to eight weeks after
something proved it wrong.
"""
import json

from pc import weekly_anchor
from pc.providers import base
from pc.providers import claude_desktop

PROVIDER_ID = "claude"
SRC_ID = "weekly_anchor"


class DesktopHistoryProvider:
    """Adapts Claude Desktop's own usage cache into the weekly-percentage
    sample series `weekly_anchor.refuted_by` needs.

    There is exactly one parser for plan-usage-history.json in this
    codebase, in pc.providers.claude_desktop -- this borrows its cache_path()
    and its raw_samples() rather than growing a second reader for the same
    file. refuted_by wants the whole series (to notice a drop), not the
    single newest reading ClaudeDesktopProvider itself hands the normalizer,
    which is why this goes to raw_samples() instead of duplicating that
    provider outright.

    Lazy on purpose: the file is opened fresh inside samples(), never at
    construction, so a machine with no Claude Desktop installed -- a normal,
    common state -- is not a broken provider, just an absent one. Every
    failure here (a missing file, a locked one, invalid JSON, an
    unrecognised shape) returns None, meaning "no evidence either way", not
    "refuted" and not "confirmed" -- refuted_by treats it exactly that way.
    """

    def __init__(self, path=None):
        self._path = path

    def samples(self, now_epoch):
        path = (self._path if self._path is not None
                else claude_desktop.cache_path())
        try:
            with open(path, "r", encoding="utf-8") as fh:
                content = fh.read()
        except OSError:
            return None
        try:
            doc = json.loads(content)
        except (TypeError, ValueError):
            return None
        return claude_desktop.raw_samples(doc)


class WeeklyAnchorProvider(base.ProviderParser):
    def __init__(self, path=None, history_provider=None):
        self._path = path
        # Optional: an object with samples(now_epoch) -> list, in the same
        # shape plan-usage-history.json's own "samples" array uses (each a
        # dict with "t" and a "u" object carrying "sd"). This is the only
        # thing refuted_by needs and this provider has no other way to see
        # it -- the anchor file itself carries no percentages. Absent, the
        # anchor simply cannot be refuted here and this behaves exactly as
        # it did before refutation was wired in: only the eight-week timeout
        # can withdraw it.
        self._history_provider = history_provider

    def get_provider_id(self) -> str:
        return PROVIDER_ID

    def path(self):
        return (self._path if self._path is not None
                else weekly_anchor.anchor_path())

    def poll(self, now_epoch):
        try:
            anchor = weekly_anchor.load(self.path())
            resets_at = weekly_anchor.project(anchor, now_epoch)
        except Exception:
            return []
        if resets_at is None:
            return []
        if self._history_provider is not None:
            try:
                samples = self._history_provider.samples(now_epoch)
            except Exception:
                # A broken sample source cannot refute anything. It also
                # must not cost the projection itself -- refuted_by is a
                # bonus check on top of an anchor that is otherwise good.
                samples = None
            try:
                if samples and weekly_anchor.refuted_by(
                        anchor, samples, now_epoch):
                    return []
            except Exception:
                pass
        return [base.NormalizedUsageFrame(
            provider=PROVIDER_ID, src=SRC_ID,
            observed_at=anchor["observed_at"],
            weekly_resets_at=resets_at)]
