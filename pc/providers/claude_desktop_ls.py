"""Claude Desktop's Local Storage, as a provider on the ingestion bus.

A thin adapter on purpose. Everything about the record's shape and the
store's framing lives in pc/desktop_local_storage and stays there.

This source exists for one configuration: Claude Desktop with no Claude Code.
Those customers have percentages from plan-usage-history.json and, until now,
no reset time at all -- which is why pc/providers/claude_desktop grew a burn
rate. This gives them a real countdown. The burn rate stays: it answers a
different question and it survives this source being absent.
"""
from pc import desktop_local_storage as ls
from pc.providers import base

PROVIDER_ID = "claude"
SRC_ID = "desktop_ls"

# Same bound, and the same reason, as pc/providers/claude_desktop: the record
# is rewritten when the app acts, so age here measures how long ago someone
# used it rather than how wrong the number is.
STALE_AFTER_S = 1800


class ClaudeDesktopLocalStorageProvider(base.ProviderParser):
    def __init__(self, dir_path=None, stale_after_s=STALE_AFTER_S):
        self._dir = dir_path
        self._stale_after = stale_after_s

    def get_provider_id(self) -> str:
        return PROVIDER_ID

    def path(self):
        return self._dir if self._dir is not None else ls.store_path()

    def record_to_frame(self, rec, now_epoch):
        """A NormalizedUsageFrame from one record, or None."""
        if not isinstance(rec, dict):
            return None
        try:
            util = float(rec["utilization"])
            observed_at = float(rec["observedAt"])
        except (KeyError, TypeError, ValueError):
            return None

        pct = util * 100.0
        # Upper bound 1000, not 100: overage carries a window past its limit
        # and 102 is a real reading, not a changed field.
        if not (0 <= pct <= 1000):
            pct = base.UNKNOWN

        resets_at = rec.get("resetsAt")
        if not isinstance(resets_at, (int, float)) or isinstance(
                resets_at, bool):
            resets_at = None
        elif not (ls.SAMPLE_EPOCH_MIN <= resets_at <= ls.SAMPLE_EPOCH_MAX):
            resets_at = None

        rolled_at = None
        if resets_at is not None and now_epoch >= resets_at:
            # The window has rolled. The stored percentage describes a window
            # that no longer exists, and the record will not be rewritten
            # until the app next acts. Report the rollover as evidence and
            # publish no number -- see this module's tests.
            rolled_at = resets_at
            resets_at = None
            pct = base.UNKNOWN

        return base.NormalizedUsageFrame(
            provider=PROVIDER_ID, src=SRC_ID, observed_at=observed_at,
            session_pct=pct, session_resets_at=resets_at,
            session_rolled_at=rolled_at,
            stale=(now_epoch - observed_at) > self._stale_after)

    def poll(self, now_epoch):
        try:
            rec = ls.newest_record(self.path())
        except Exception:
            # An ambient source over an application we do not control. Its
            # absence and its breakage are both normal and both silent.
            return []
        if rec is None:
            return []
        frame = self.record_to_frame(rec, now_epoch)
        return [frame] if frame is not None else []
