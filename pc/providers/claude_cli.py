"""Claude Code's status line, as a provider on the ingestion bus.

A thin adapter, on purpose. All of the reasoning about this source -- the
freshness bound and why age is the wrong primary signal, carrying a window
across its own reset, why an absent window is -1 and not 0 -- lives in
pc/statusline_source and stays there. Re-implementing any of it here would
give the project two rules for the same file.

This briefly read one payload per session, to take the worst of several
context windows and name the model in use. Both came off the panel -- with
several agents running there are several contexts at different levels and no
single number is any of them -- and what is left is account-wide: every open
terminal reports the same two percentages, so the most recent render is as
good as any.
"""
from pc import statusline_source as ss
from pc.providers import base

PROVIDER_ID = ss.PROVIDER_ID
SRC_ID = ss.SRC_ID


class ClaudeCliProvider(base.ProviderParser):
    def __init__(self, path=None):
        self._path = path if path is not None else ss.PAYLOAD_PATH

    def get_provider_id(self) -> str:
        return PROVIDER_ID

    def path(self):
        return self._path

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

    def poll(self, now_epoch):
        payload, mtime = ss.read_payload(self._path)
        if payload is None:
            return []
        frame = self.parse_cli_event(payload, now_epoch, mtime)
        return [frame] if frame is not None else []
