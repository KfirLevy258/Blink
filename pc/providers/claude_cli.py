"""Claude Code's status line, as a provider on the ingestion bus.

All of the reasoning about a single payload -- the freshness bound and why age
is the wrong primary signal, carrying a window across its own reset, why an
absent window is -1 and not 0 -- lives in pc/statusline_source and stays there.
What lives HERE is the part that only exists because there can be several
sessions at once.

Two of the fields are per-session and the rest are not. Session and weekly
percentages describe an account, so every open terminal reports the same two
numbers and the freshest copy is as good as any. The CONTEXT WINDOW and the
model describe one conversation, and with four agents running there are four
context windows and no single number is all of them.

So context uses a different rule from everything else on the bus: the WORST,
not the freshest. The fullest context is the one about to end somebody's turn,
and that is the one worth a pixel. The count travels with it so the panel can
say "88% of 4" rather than letting one number pass as the only one.
"""
import os
import time

from pc import statusline_source as ss
from pc.providers import base

PROVIDER_ID = ss.PROVIDER_ID
SRC_ID = ss.SRC_ID

STATUSLINE_DIR = os.path.expanduser("~/.clauge/statusline")

# A session whose status line has not been rendered for this long is gone or
# idle; its files are collected so the directory does not grow forever. Matched
# to the state directory's threshold for the same reason.
ABANDONED_AFTER_S = 3600.0


class ClaudeCliProvider(base.ProviderParser):
    def __init__(self, path=None, sweep=True):
        self._dir = path if path is not None else STATUSLINE_DIR
        self._sweep = sweep

    def get_provider_id(self) -> str:
        return PROVIDER_ID

    def path(self):
        return self._dir

    def parse_cli_event(self, raw_payload, now_epoch, observed_at):
        """A push from Claude Code, already read.

        `observed_at` is the payload's mtime rather than now: the file is
        written when Claude Code renders, so its age is the reading's age.
        Passing now() here would make every reading look current, which is
        the one thing the freshness logic exists to prevent.
        """
        if not isinstance(raw_payload, dict):
            return None
        return ss.map_statusline_frame(raw_payload, now_epoch, observed_at)

    def _frames(self, now_epoch):
        """One frame per live session, freshest first."""
        try:
            names = sorted(os.listdir(self._dir))
        except OSError:
            return []

        out = []
        for name in names:
            if not name.endswith(".json"):
                continue
            path = os.path.join(self._dir, name)
            payload, mtime = ss.read_payload(path)
            if payload is None:
                continue
            if now_epoch - mtime > ABANDONED_AFTER_S:
                # Not merely stale -- gone. A stale session still has usage
                # windows worth showing; one nobody has rendered in an hour
                # has been closed, and its file is the only thing left of it.
                if self._sweep:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                continue
            frame = self.parse_cli_event(payload, now_epoch, mtime)
            if frame is not None:
                out.append(frame)
        out.sort(key=lambda f: f.observed_at, reverse=True)
        return out

    def poll(self, now_epoch):
        frames = self._frames(now_epoch)
        if not frames:
            return []

        # Account-wide fields come from the freshest session; every session
        # reports the same two windows, so this is a tie-break, not a choice.
        primary = frames[0]

        # Context is the exception, and the whole reason this class exists.
        contexts = [f.ctx_pct for f in frames if f.ctx_pct >= 0]
        if contexts:
            primary.ctx_pct = max(contexts)
            primary.n_ctx = len(contexts)
        else:
            primary.ctx_pct = base.UNKNOWN
            primary.n_ctx = 0
        return [primary]
