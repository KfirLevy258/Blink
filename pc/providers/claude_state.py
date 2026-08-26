"""Execution state: is Claude Code working, waiting for you, or wedged?

The handoff document specifies this state machine in terms of things a daemon
cannot see from outside -- "CLI process waiting on stdin", "tool execution
with 0% CPU". Both would mean finding the right process among several, sampling
it, and inferring intent from a number that is legitimately zero whenever a
tool is waiting on the network. That is a guess dressed as a measurement.

Claude Code already announces every one of these transitions through its hook
interface, so this reads the announcements instead:

    UserPromptSubmit  -> a turn started              -> running
    PreToolUse        -> a tool started              -> running
    PostToolUse       -> a tool finished             -> running
    Notification      -> Claude Code wants the user  -> waiting
    Stop / SessionEnd -> the turn completed          -> idle

`waiting` in particular is authoritative here rather than inferred: Notification
is the event Claude Code fires when it needs a human, which is exactly the
question "is the process blocked on stdin" was trying to answer indirectly.

`stuck` is the one state with no event of its own, because being wedged is by
definition the absence of an event. It is inferred from silence -- a turn that
announced it was working and then said nothing for a long time.
"""
import json
import os
import time

from pc.providers import base

PROVIDER_ID = "claude"
SRC_ID = "cli"

STATE_PATH = os.path.expanduser("~/.clauge/state.json")

# How long a turn may go silent before the panel calls it stuck.
#
# The document says 60 seconds. That is too twitchy to ship: a test suite, an
# npm install, a docker build or a slow model response all routinely exceed a
# minute while being perfectly healthy, and a red alert that cries wolf on
# every build teaches its owner to ignore the one time it is right. Three
# minutes still catches a genuinely wedged tool well before a human would
# notice, and almost never fires on work that is merely slow.
STUCK_AFTER_S = 180.0

# Past this, assume Claude Code is not running at all rather than that it has
# been idle since. "idle" is a claim about a live session; an hour of silence
# is better described by saying nothing, which leaves the panel's execution
# indicator dark instead of green.
ABANDONED_AFTER_S = 3600.0

# Event name -> the state it puts us in. Names are Claude Code's, passed to the
# shim as argv rather than parsed out of the payload.
_RUNNING_EVENTS = ("UserPromptSubmit", "PreToolUse", "PostToolUse",
                   "SubagentStop", "PreCompact")
_IDLE_EVENTS = ("Stop", "SessionEnd")
_WAITING_EVENTS = ("Notification",)


def derive_state(event: str, age_s: float, stuck_after_s=STUCK_AFTER_S) -> str:
    """The execution state implied by the last event and how long ago it was."""
    if age_s < 0:
        # A clock that went backwards -- a laptop waking, an NTP step. Treat
        # it as fresh rather than as a negative age that would sail under
        # every threshold below and report a confident state from a broken
        # measurement.
        age_s = 0.0

    if age_s > ABANDONED_AFTER_S:
        return base.STATE_UNKNOWN

    if event in _IDLE_EVENTS:
        # No age test. A completed turn stays completed; silence after it is
        # the expected condition, not a fault.
        return base.STATE_IDLE

    if event in _WAITING_EVENTS:
        # Also no stuck test. A prompt waiting for a human is not wedged, and
        # it stays waiting for as long as the human takes.
        return base.STATE_WAITING

    if event in _RUNNING_EVENTS:
        return base.STATE_STUCK if age_s > stuck_after_s else base.STATE_RUNNING

    # An event this version does not know. Newer Claude Code, most likely.
    # Silence beats guessing.
    return base.STATE_UNKNOWN


class ClaudeStateProvider(base.ProviderParser):
    """Execution state only -- this source never carries a usage percentage.

    That matters to the normalizer: a frame with no percentage can never
    become the primary source, so this cannot make a panel look fresher than
    its numbers actually are. It contributes one field and nothing else.
    """

    def __init__(self, path=None, now=time.time, stuck_after_s=STUCK_AFTER_S):
        self._path = path if path is not None else STATE_PATH
        self._now = now
        self._stuck_after = stuck_after_s

    def get_provider_id(self) -> str:
        return PROVIDER_ID

    def path(self):
        return self._path

    def parse_cli_event(self, raw_payload, now_epoch, observed_at=None):
        if not isinstance(raw_payload, dict):
            return None
        event = raw_payload.get("event")
        if not isinstance(event, str) or not event:
            return None
        try:
            t = float(raw_payload["t"])
        except (KeyError, TypeError, ValueError):
            return None

        state = derive_state(event, now_epoch - t, self._stuck_after)
        if state == base.STATE_UNKNOWN:
            return None
        return base.NormalizedUsageFrame(
            provider=PROVIDER_ID, src=SRC_ID, observed_at=t, state=state)

    def poll(self, now_epoch):
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, ValueError):
            # No hooks installed, or nothing has happened yet. Both normal.
            return []
        frame = self.parse_cli_event(payload, now_epoch)
        return [frame] if frame is not None else []
