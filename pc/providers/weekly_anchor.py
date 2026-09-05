"""The remembered seven-day boundary, offered as a frame.

Contributes one field and dates it honestly. `observed_at` is when the
boundary was ORIGINALLY seen, not now, so pc/normalizer -- which ranks by
recency per field -- prefers any live source without needing to know this
provider exists.
"""
from pc import weekly_anchor
from pc.providers import base

PROVIDER_ID = "claude"
SRC_ID = "weekly_anchor"


class WeeklyAnchorProvider(base.ProviderParser):
    def __init__(self, path=None, history_provider=None):
        self._path = path
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
        return [base.NormalizedUsageFrame(
            provider=PROVIDER_ID, src=SRC_ID,
            observed_at=anchor["observed_at"],
            weekly_resets_at=resets_at)]
